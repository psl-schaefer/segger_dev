import os
import torch
import pandas as pd
import numpy as np
import torch.nn.functional as F
import torch._dynamo
from torch_geometric.loader import DataLoader
from torch_geometric.data import Batch
from torchmetrics import F1Score
from scipy.sparse.csgraph import connected_components as cc

from segger.data.utils import (
    SpatialTranscriptomicsDataset,
    get_edge_index,
    coo_to_dense_adj,
)
from segger.data.io import XeniumSample
from segger.models.segger_model import Segger
from segger.training.train import LitSegger
from segger.training.segger_data_module import SeggerDataModule
from lightning import LightningModule
from torch_geometric.nn import to_hetero
import random
import string
import os
import yaml
from pathlib import Path
import glob
import typing
import re
from tqdm import tqdm
from segger.data.utils import create_anndata
import dask.dataframe as dd
import dask
import pandas as pd
from dask import delayed
from typing import Union, Optional
import anndata as ad


# CONFIG
torch._dynamo.config.suppress_errors = True
os.environ["PYTORCH_USE_CUDA_DSA"] = "1"
os.environ["CUDA_LAUNCH_BLOCKING"] = "1"

def load_model(checkpoint_path: str) -> LitSegger:
    """
    Load a LitSegger model from a checkpoint.

    Parameters
    ----------
    checkpoint_path : os.Pathlike
        Specific checkpoint file to load, or directory where the model 
        checkpoints are stored. If directory, the latest checkpoint is loaded.

    Returns
    -------
    LitSegger
        The loaded LitSegger model.

    Raises
    ------
    FileNotFoundError
        If the specified checkpoint file does not exist.
    """
    # Get last checkpoint if directory provided
    checkpoint_path = Path(checkpoint_path)
    msg = (
        f"No checkpoint found at {checkpoint_path}. Please make sure "
        "you've provided the correct path."
    )
    if os.path.isdir(checkpoint_path):
        checkpoints = glob.glob(str(checkpoint_path / '*.ckpt'))
        if len(checkpoints) == 0:
            raise FileNotFoundError(msg)
        def sort_order(c):
            match = re.match(r'.*epoch=(\d+)-step=(\d+).ckpt', c)
            return int(match[1]), int(match[2])
        checkpoint_path = Path(sorted(checkpoints, key=sort_order)[-1])
    elif not checkpoint_path.exists():
        raise FileExistsError(msg)

    # Load model
    lit_segger = LitSegger.load_from_checkpoint(
        checkpoint_path=checkpoint_path,
        #map_location=torch.device("cuda"),
    )

    return lit_segger


def get_similarity_scores(
    model: Segger, 
    batch: Batch,
    from_type: str,
    to_type: str,
):
    """
    Compute similarity scores between 'from_type' and 'to_type' embeddings 
    within a batch.

    Parameters
    ----------
    model : Segger
        The segmentation model used to generate embeddings.
    batch : Batch
        A batch of data containing input features and edge indices.
    from_type : str
        The type of node from which the similarity is computed.
    to_type : str
        The type of node to which the similarity is computed.

    Returns
    -------
    torch.Tensor
        A dense tensor containing the similarity scores between 'from_type' 
        and 'to_type' nodes.
    """
    # Get embedding spaces from model
    batch = batch.to("cuda")
    y_hat = model(batch.x_dict, batch.edge_index_dict)

    # Similarity of each 'from_type' to 'to_type' neighbors in embedding
    nbr_idx = batch[from_type][f'{to_type}_field']
    m = torch.nn.ZeroPad2d((0, 0, 0, 1))  # pad bottom with zeros
    similarity = torch.bmm(
        m(y_hat[to_type])[nbr_idx],    # 'to' x 'from' neighbors x embed
        y_hat[from_type].unsqueeze(-1) # 'to' x embed x 1
    )                                  # -> 'to' x 'from' neighbors x 1

    # Sigmoid to get most similar 'to_type' neighbor
    similarity[similarity == 0] = -torch.inf  # ensure zero stays zero
    similarity = F.sigmoid(similarity)

    # Neighbor-filtered similarity scores
    shape = batch[from_type].x.shape[0], batch[to_type].x.shape[0]
    indices =  torch.argwhere(nbr_idx != -1).T
    indices[1] = nbr_idx[nbr_idx != -1]
    values = similarity[nbr_idx != -1].flatten()
    sparse_sim = torch.sparse_coo_tensor(indices, values, shape)

    # Return in dense format for backwards compatibility
    scores = sparse_sim.to_dense().detach().cpu()

    return scores


def predict_batch(
    lit_segger: LitSegger,
    batch: Batch,
    score_cut: float,
    receptive_field: dict,
    use_cc: bool = True,
) -> pd.DataFrame:
    """
    Predict cell assignments for a batch of transcript data using a 
    segmentation model.

    Parameters
    ----------
    lit_segger : LitSegger
        The lightning module wrapping the segmentation model.
    batch : Batch
        A batch of transcript and cell data.
    score_cut : float
        The threshold for assigning transcripts to cells based on similarity 
        scores.

    Returns
    -------
    pd.DataFrame
        A DataFrame containing the transcript IDs, similarity scores, and 
        assigned cell IDs.
    """
    # Get random Xenium-style ID
    def _get_id():
        id_chars = random.choices(string.ascii_lowercase, k=8)
        return ''.join(id_chars) + '-nx'
    
    with torch.no_grad():

        batch = batch.to("cuda")

        # Assignments of cells to nuclei
        assignments = pd.DataFrame()
        assignments['transcript_id'] = batch['tx'].id.cpu().numpy()

        if len(batch['bd'].id[0]) > 0:
            # Transcript-cell similarity scores, filtered by neighbors
            edge_index = get_edge_index(
                batch['bd'].pos[:, :2].cpu(),
                batch['tx'].pos[:, :2].cpu(),
                k=receptive_field['k_bd'],
                dist=receptive_field['dist_bd'],
                method='kd_tree',
            ).T
            batch['tx']['bd_field'] = coo_to_dense_adj(
                edge_index,
                num_nodes=batch['tx'].id.shape[0],
                num_nbrs=receptive_field['k_bd'],
            )
            scores = get_similarity_scores(lit_segger.model, batch, "tx", "bd")
            # 1. Get direct assignments from similarity matrix
            belongs = scores.max(1)
            assignments['score'] = belongs.values.cpu()
            mask = assignments['score'] > score_cut
            all_ids = np.concatenate(batch['bd'].id)[belongs.indices.cpu()]
            assignments.loc[mask, 'segger_cell_id'] = all_ids[mask]

            if use_cc:
                # Transcript-transcript similarity scores, filtered by neighbors
                edge_index = batch['tx', 'neighbors', 'tx'].edge_index
                batch['tx']['tx_field'] = coo_to_dense_adj(
                    edge_index,
                    num_nodes=batch['tx'].id.shape[0],
                )
                scores = get_similarity_scores(lit_segger.model, batch, "tx", "tx")
                scores = scores.fill_diagonal_(0)  # ignore self-similarity

                # 2. Assign remainder using connected components
                no_id = assignments['segger_cell_id'].isna().values
                no_id_scores = scores[no_id][:, no_id]
                print('here')
                n, comps = cc(no_id_scores, connection="weak", directed=False)
                new_ids = np.array([_get_id() for _ in range(n)])
                assignments.loc[no_id, 'segger_cell_id'] = new_ids[comps]

        return assignments


def predict(
    lit_segger: LitSegger,
    data_loader: DataLoader,
    score_cut: float,
    receptive_field: dict,
    use_cc: bool = True,
) -> pd.DataFrame:
    """
    Predict cell assignments for multiple batches of transcript data using 
    a segmentation model.

    Parameters
    ----------
    lit_segger : LitSegger
        The lightning module wrapping the segmentation model.
    data_loader : DataLoader
        A data loader providing batches of transcript and cell data.
    score_cut : float
        The threshold for assigning transcripts to cells based on similarity 
        scores.

    Returns
    -------
    pd.DataFrame
        A DataFrame containing the transcript IDs, similarity scores, and 
        assigned cell IDs, consolidated across all batches.
    """
    # If data loader is empty, do nothing
    if len(data_loader) == 0:
        return None
    
    assignments = []

    # Assign transcripts from each batch to nuclei
    # TODO: parallelize this step
    for batch in tqdm(data_loader):
        batch_assignments = predict_batch(
            lit_segger, batch, score_cut, receptive_field, use_cc
        )
        assignments.append(batch_assignments)

    # Join across batches and handle duplicates between batches
    assignments = pd.concat(assignments).reset_index(drop=True)

    # Handle duplicate assignments of transcripts
    idx = assignments.groupby('transcript_id')['score'].idxmax()
    assignments = assignments.loc[idx].reset_index(drop=True)

    return assignments


def segment(
    model: LitSegger, 
    dm: SeggerDataModule, 
    save_dir: Union[str, Path], 
    seg_tag: str, 
    transcript_file: Union[str, Path], 
    file_format: str = 'anndata', 
    receptive_field: dict =  {'k_bd': 4, 'dist_bd': 10, 'k_tx': 5, 'dist_tx': 3},
    **anndata_kwargs
) -> None:
    """
    Perform segmentation using the model, merge segmentation results with transcripts_df, and save in the specified format.
    
    Parameters:
    ----------
    model : LitSegger
        The trained segmentation model.
    dm : SeggerDataModule
        The SeggerDataModule instance for data loading.
    save_dir : Union[str, Path]
        Directory to save the final segmentation results.
    seg_tag : str
        Tag to include in the saved filename.
    transcript_file : Union[str, Path]
        Path to the transcripts parquet file.
    file_format : str, optional
        File format to save the results ('csv', 'parquet', or 'anndata'). Defaults to 'anndata'.
    **anndata_kwargs : dict, optional
        Additional keyword arguments passed to the `create_anndata` function, such as:
        - panel_df: pd.DataFrame
        - min_transcripts: int
        - cell_id_col: str
        - qv_threshold: float
        - min_cell_area: float
        - max_cell_area: float
        
    Returns:
    -------
    None
    """
    # Ensure the save directory exists
    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    
    # Define delayed prediction steps for parallel execution
    delayed_train = delayed(predict)(model, dm.train_dataloader(), score_cut=0.5, receptive_field=receptive_field, use_cc=True)
    delayed_val = delayed(predict)(model, dm.val_dataloader(), score_cut=0.5, receptive_field=receptive_field, use_cc=True)
    delayed_test = delayed(predict)(model, dm.test_dataloader(), score_cut=0.5, receptive_field=receptive_field, use_cc=True)
    
    # Trigger parallel execution and get results
    segmentation_train, segmentation_val, segmentation_test = dask.compute(delayed_train, delayed_val, delayed_test)

    # Combine the segmentation results
    seg_combined = pd.concat([segmentation_train, segmentation_val, segmentation_test]).reset_index()

    # Group by transcript_id and keep the row with the highest score
    seg_final = seg_combined.loc[seg_combined.groupby('transcript_id')['score'].idxmax()]

    # Drop rows where segger_cell_id is NaN
    seg_final = seg_final.dropna(subset=['segger_cell_id'])

    # Reset the index
    seg_final.reset_index(drop=True, inplace=True)

    # Load the transcript data
    transcripts_df = dd.read_parquet(transcript_file)

    # Convert seg_final to a Dask DataFrame and merge with transcripts
    seg_final_dd = dd.from_pandas(seg_final, npartitions=transcripts_df.npartitions)
    transcripts_df_filtered = transcripts_df.merge(seg_final_dd, on='transcript_id', how='inner')

    # Compute the final result
    transcripts_df_filtered = transcripts_df_filtered.compute()

    # Save the merged result based on the file format
    if file_format == 'csv':
        save_path = save_dir / f'{seg_tag}_segmentation.csv'
        transcripts_df_filtered.to_csv(save_path, index=False)
    elif file_format == 'parquet':
        save_path = save_dir / f'{seg_tag}_segmentation.parquet'
        transcripts_df_filtered.to_parquet(save_path, index=False)
    elif file_format == 'anndata':
        # Create an AnnData object and save as h5ad, passing additional arguments from kwargs
        save_path = save_dir / f'{seg_tag}_segmentation.h5ad'
        segger_adata = create_anndata(transcripts_df_filtered, **anndata_kwargs)
        segger_adata.write(save_path)
    else:
        raise ValueError(f"Unsupported file format: {file_format}")

    print(f"Segmentation results saved at {save_path}")