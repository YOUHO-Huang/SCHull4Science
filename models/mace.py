from typing import Optional

from torch_cluster import radius_graph

import torch
from torch.nn import functional as F
from torch_geometric.nn import global_add_pool, global_mean_pool
import e3nn

from mace_modules.irreps_tools import reshape_irreps
from mace_modules.blocks import (
    EquivariantProductBasisBlock,
    RadialEmbeddingBlock,
)
from layers.tfn_layer import TensorProductConvLayer
num_aa_type = 26
num_bb_embs = 6

class MACE(torch.nn.Module):
    """
    MACE model from "MACE: Higher Order Equivariant Message Passing Neural Networks".
    """
    def __init__(
        self,
        r_max: float = 10.0,
        num_bessel: int = 8,
        num_polynomial_cutoff: int = 5,
        max_ell: int = 1,
        correlation: int = 3,
        num_layers: int = 5,
        emb_dim: int = 16,
        hidden_irreps: Optional[e3nn.o3.Irreps] = None,
        mlp_dim: int = 256,
        out_channels: int = 1195,
        out_layers: int = 2,
        in_dim: int = 1,
        out_dim: int = 1,
        dropout: float = 0.0,
        aggr: str = "sum",
        pool: str = "sum",
        batch_norm: bool = True,
        residual: bool = True,
        equivariant_pred: bool = False,
        schull: bool = False
    ):
        """
        Parameters:
        - r_max (float): Maximum distance for Bessel basis functions (default: 10.0)
        - num_bessel (int): Number of Bessel basis functions (default: 8)
        - num_polynomial_cutoff (int): Number of polynomial cutoff basis functions (default: 5)
        - max_ell (int): Maximum degree of spherical harmonics basis functions (default: 2)
        - correlation (int): Local correlation order = body order - 1 (default: 3)
        - num_layers (int): Number of layers in the model (default: 5)
        - emb_dim (int): Scalar feature embedding dimension (default: 64)
        - hidden_irreps (Optional[e3nn.o3.Irreps]): Hidden irreps (default: None)
        - mlp_dim (int): Dimension of MLP for computing tensor product weights (default: 256)
        - in_dim (int): Input dimension of the model (default: 1)
        - out_dim (int): Output dimension of the model (default: 1)
        - aggr (str): Aggregation method to be used (default: "sum")
        - pool (str): Global pooling method to be used (default: "sum")
        - batch_norm (bool): Whether to use batch normalization (default: True)
        - residual (bool): Whether to use residual connections (default: True)
        - equivariant_pred (bool): Whether it is an equivariant prediction task (default: False)

        Note:
        - If `hidden_irreps` is None, the irreps for the intermediate features are computed 
          using `emb_dim` and `max_ell`.
        - The `equivariant_pred` parameter determines whether it is an equivariant prediction task.
          If set to True, equivariant prediction will be performed.
        """
        super().__init__()
        
        self.r_max = r_max
        self.max_ell = max_ell
        self.num_layers = num_layers
        self.emb_dim = emb_dim
        self.mlp_dim = mlp_dim
        self.residual = residual
        self.batch_norm = batch_norm
        self.hidden_irreps = hidden_irreps
        self.equivariant_pred = equivariant_pred

        # Edge embedding
        self.radial_embedding = RadialEmbeddingBlock(
            r_max=r_max,
            num_bessel=num_bessel,
            num_polynomial_cutoff=num_polynomial_cutoff,
        )
        sh_irreps = e3nn.o3.Irreps.spherical_harmonics(max_ell)
        self.spherical_harmonics = e3nn.o3.SphericalHarmonics(
            sh_irreps, normalize=True, normalization="component"
        )

        # Embedding lookup for initial node features
        # self.emb_in = torch.nn.Embedding(in_dim, emb_dim)
        self.emb_in = torch.nn.Linear(num_aa_type + num_bb_embs, emb_dim)
        
        # Set hidden irreps if none are provided
        if hidden_irreps is None:
            hidden_irreps = (sh_irreps * emb_dim).sort()[0].simplify()
            # Note: This defaults to O(3) equivariant layers
            # It is possible to use SO(3) equivariance by passing the appropriate irreps

        self.convs = torch.nn.ModuleList()
        self.prods = torch.nn.ModuleList()
        self.reshapes = torch.nn.ModuleList()
        
        # First layer: scalar only -> tensor
        self.convs.append(
            TensorProductConvLayer(
                in_irreps=e3nn.o3.Irreps(f'{emb_dim}x0e'),
                out_irreps=hidden_irreps,
                sh_irreps=sh_irreps,
                edge_feats_dim=self.radial_embedding.out_dim,
                mlp_dim=mlp_dim,
                aggr=aggr,
                batch_norm=batch_norm,
                gate=False,
            )
        )
        self.reshapes.append(reshape_irreps(hidden_irreps))
        self.prods.append(
            EquivariantProductBasisBlock(
                node_feats_irreps=hidden_irreps,
                target_irreps=hidden_irreps,
                correlation=correlation,
                element_dependent=False,
                num_elements=in_dim,
                use_sc=residual
            )
        )

        # Intermediate layers: tensor -> tensor
        for _ in range(num_layers - 1):
            self.convs.append(
                TensorProductConvLayer(
                    in_irreps=hidden_irreps,
                    out_irreps=hidden_irreps,
                    sh_irreps=sh_irreps,
                    edge_feats_dim=self.radial_embedding.out_dim,
                    mlp_dim=mlp_dim,
                    aggr=aggr,
                    batch_norm=batch_norm,
                    gate=False,
                )
            )
            self.reshapes.append(reshape_irreps(hidden_irreps))
            self.prods.append(
                EquivariantProductBasisBlock(
                    node_feats_irreps=hidden_irreps,
                    target_irreps=hidden_irreps,
                    correlation=correlation,
                    element_dependent=False,
                    num_elements=in_dim,
                    use_sc=residual
                )
            )

        # Global pooling/readout function
        self.pool = {"mean": global_mean_pool, "sum": global_add_pool}[pool]

        if self.equivariant_pred:
            # Linear predictor for equivariant tasks using geometric features
            self.pred = torch.nn.Linear(hidden_irreps.dim, out_dim)
        else:
            # MLP predictor for invariant tasks using only scalar features
            self.pred = torch.nn.Sequential(
                torch.nn.Linear(emb_dim, emb_dim),
                torch.nn.ReLU(),
                torch.nn.Linear(emb_dim, out_dim)
            )

        self.lins_out = torch.nn.ModuleList()
        self.lins_out.append(torch.nn.Linear(64, mlp_dim))
        self.lin_out = torch.nn.Linear(mlp_dim, out_channels)
        self.relu = torch.nn.ReLU()
        self.dropout = torch.nn.Dropout(dropout)
        self.schull = schull  
    
    def forward(self, batch):
        # Node embedding
        z, pos = torch.squeeze(batch.x.long()), batch.coords_ca
        bb_embs = batch.bb_embs
        x = torch.cat([torch.squeeze(F.one_hot(z, num_classes=num_aa_type).float()), bb_embs], dim = 1)
        h = self.emb_in(x)  # (n,) -> (n, d)

        # Edge embedding
        
        edge_index = radius_graph(pos, r=self.r_max, batch=batch.batch, max_num_neighbors=32)
        vectors = pos[edge_index[0]] - pos[edge_index[1]]  # [n_edges, 3]
        lengths = torch.linalg.norm(vectors, dim=-1, keepdim=True)  # [n_edges, 1]
        edge_sh = self.spherical_harmonics(vectors)
        edge_feats = self.radial_embedding(lengths)
        if self.schull:
            ch_edge_index = batch['ch_edge_index'].long()
            ch_pos = batch['ch_pos']
            ch_vectors = ch_pos[ch_edge_index[0]] - ch_pos[ch_edge_index[1]]
            ch_lengths = torch.linalg.norm(ch_vectors, dim=-1, keepdim=True)
            ch_edge_sh = self.spherical_harmonics(ch_vectors)
            ch_edge_feats = self.radial_embedding(ch_lengths)
            edge_index = torch.cat((edge_index, ch_edge_index), dim=1)
            edge_sh = torch.cat((edge_sh, ch_edge_sh), dim=0)
            edge_feats = torch.cat((edge_feats, ch_edge_feats), dim=0)
        
        for conv, reshape, prod in zip(self.convs, self.reshapes, self.prods):
            # Message passing layer
            h_update = conv(h, edge_index, edge_sh, edge_feats)
            # Update node features
            sc = F.pad(h, (0, h_update.shape[-1] - h.shape[-1]))
            h = prod(reshape(h_update), sc, None)
            

        out = self.pool(h, batch.batch)  # (n, d) -> (batch_size, d)
        # if not self.equivariant_pred:
        #     # Select only scalars for invariant prediction
        #     out = out[:,:self.emb_dim]
        
        for lin in self.lins_out:
            out = self.relu(lin(out))
            out = self.dropout(out)  
        out = self.lin_out(out)

        return out
