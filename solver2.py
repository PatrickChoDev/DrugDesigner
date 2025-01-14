import numpy as np
import os
import os.path as osp
import time
import datetime
import torch.nn
import torch
import torch.nn.functional as F
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.tensorboard import SummaryWriter
from utils import *
from models import (
    Generator,
    Discriminator,
    Generator2,
    Discriminator2,
    Discriminator_old,
    Discriminator_old2,
    Discriminator3,
    PNA_Net,
)
import torch_geometric.utils as geoutils
import wandb
from torch_geometric.loader import DataLoader
from new_dataloader import DruggenDataset
from new_dataloader_drugs import DruggenDataset_drugs
import torch.utils.data
from torch.nn.parallel import DataParallel as DP
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import seaborn as sns


class Solver2(object):
    """Solver for training and testing DrugDesigner."""

    def __init__(self, config):
        """Initialize configurations."""
        self.dataset_file = config.dataset_file

        self.dataset_name = self.dataset_file.split(".")[0]
        self.z_dim = config.z_dim
        self.num_test_epoch = config.num_test_epoch
        # Data loader.

        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.batch_size = config.batch_size

        self.dataset = DruggenDataset(config.mol_data_dir, self.dataset_file)
        self.loader = DataLoader(
            self.dataset,
            shuffle=True,
            batch_size=self.batch_size,
            drop_last=True,
            pin_memory=True,
            num_workers=16,
        )
        self.inference_sample_num = config.inference_sample_num
        atom_decoders = self.decoder_load("atom_decoders")
        bond_decoders = self.decoder_load("bond_decoders")

        self.m_dim = len(atom_decoders)
        self.b_dim = len(bond_decoders)
        self.vertexes = int(self.loader.dataset[0].x.shape[0])

        self.drugs = DruggenDataset_drugs(config.drug_data_dir)
        self.drugs_loader = DataLoader(
            self.drugs,
            shuffle=True,
            batch_size=self.batch_size,
            drop_last=True,
            pin_memory=True,
            num_workers=16,
        )

        drugs_atom_decoders = self.drug_decoder_load("drugs_atom_decoders")
        drugs_bond_decoders = self.drug_decoder_load("drugs_bond_decoders")
        self.drugs_m_dim = len(drugs_atom_decoders)
        self.drugs_b_dim = len(drugs_bond_decoders)
        self.drug_vertexes = int(self.drugs_loader.dataset[0].x.shape[0])

        # Transformer and Convolution configurations.

        self.g_conv_dim = config.g_conv_dim
        self.d_conv_dim = config.d_conv_dim
        self.lambda_gp = config.lambda_gp
        self.post_method = config.post_method
        self.dim = config.dim
        self.depth = config.depth
        self.heads = config.heads
        self.mlp_ratio = config.mlp_ratio
        self.drop_rate = config.drop_rate
        self.metrics = config.metrics
        self.vertexes_protein = None
        self.edges_protein = None
        self.nodes_protein = None
        self.dec_depth = config.dec_depth
        self.dec_heads = config.dec_heads
        self.dis_select = config.dis_select
        self.la = config.la
        self.la2 = config.la2
        self.gcn_depth = config.gcn_depth
        # PNA config

        self.agg = config.aggregators
        self.sca = config.scalers
        self.pna_in_ch = config.pna_in_ch
        self.pna_out_ch = config.pna_out_ch
        self.edge_dim = config.edge_dim
        self.towers = config.towers
        self.pre_lay = config.pre_lay
        self.post_lay = config.post_lay
        self.pna_layer_num = config.pna_layer_num
        self.graph_add = config.graph_add

        # Training configurations.

        self.num_iters = config.num_iters
        self.num_iters_decay = config.num_iters_decay
        self.g_lr = config.g_lr
        self.d_lr = config.d_lr
        self.g2_lr = config.g2_lr
        self.d2_lr = config.d2_lr
        self.dropout = config.dropout
        self.n_critic = config.n_critic
        self.beta1 = config.beta1
        self.beta2 = config.beta2
        self.resume_iters = config.resume_iters
        self.warm_up_steps = config.warm_up_steps

        # Test configurations.

        self.test_iters = config.test_iters

        # Directories.

        self.log_dir = config.log_dir
        self.sample_dir = config.sample_dir
        self.model_save_dir = config.model_save_dir
        self.result_dir = config.result_dir
        self.degree_dir = config.degree_dir

        # Step size.

        self.log_step = config.log_step
        self.sample_step = config.sample_step
        self.model_save_step = config.model_save_step
        self.lr_update_step = config.lr_update_step
        self.clipping_value = config.clipping_value
        # Miscellaneous.

        self.mode = config.mode
        self.use_tensorboard = config.use_tensorboard

        self.noise_strength_0 = torch.nn.Parameter(torch.zeros([]))
        self.noise_strength_1 = torch.nn.Parameter(torch.zeros([]))
        self.noise_strength_2 = torch.nn.Parameter(torch.zeros([]))
        self.noise_strength_3 = torch.nn.Parameter(torch.zeros([]))
        self.deg = self._genDegree()
        self.init_type = config.init_type
        self.build_model()
        if self.use_tensorboard:
            self.build_tensorboard()

    def build_model(self):
        self.tra_conv = False
        if self.dis_select == "TraConv":
            self.tra_conv = True
        """Create generators and discriminators."""

        """ Generator is based on Transformer Encoder: 
            
            @ g_conv_dim: Dimensions for first MLP layers before Transformer Encoder
            @ vertexes: maximum length of generated molecules (atom length)
            @ b_dim: number of bond types
            @ m_dim: number of atom types (or number of features used)
            @ dropout: dropout possibility
            @ dim: Hidden dimension of Transformer Encoder
            @ depth: Transformer layer number
            @ heads: Number of multihead-attention heads
            @ mlp_ratio: Read-out layer dimension of Transformer
            @ drop_rate: depricated  
            @ tra_conv: Whether module creates output for TransformerConv discriminator
            """

        self.G = Generator(
            self.g_conv_dim,
            self.vertexes,
            self.b_dim,
            self.m_dim,
            self.dropout,
            dim=self.dim,
            depth=self.depth,
            heads=self.heads,
            mlp_ratio=self.mlp_ratio,
            drop_rate=self.drop_rate,
        )

        self.G2 = Generator2(
            dim=self.dim,
            depth=self.dec_depth,
            heads=self.dec_heads,
            mlp_ratio=self.mlp_ratio,
            drop_rate=self.drop_rate,
            drugs_m_dim=self.drugs_m_dim,
            drugs_b_dim=self.drugs_b_dim,
            b_dim=self.b_dim,
            m_dim=self.m_dim,
        )

        """ Discriminator implementation with PNA:
        
            @ deg: Degree distribution based on used data. (Created with _genDegree() function)
            @ agg: aggregators used in PNA
            @ sca: scalers used in PNA
            @ pna_in_ch: First PNA hidden dimension
            @ pna_out_ch: Last PNA hidden dimension
            @ edge_dim: Edge hidden dimension
            @ towers: Number of towers (Splitting the hidden dimension to multiple parallel processes)
            @ pre_lay: Pre-transformation layer
            @ post_lay: Post-transformation layer 
            @ pna_layer_num: number of PNA layers
            @ graph_add: global pooling layer selection
            """
        self.D_TraConv = Discriminator3(self.dim)
        self.D_PNA = Discriminator(
            self.deg,
            self.agg,
            self.sca,
            self.pna_in_ch,
            self.pna_out_ch,
            self.edge_dim,
            self.towers,
            self.pre_lay,
            self.post_lay,
            self.pna_layer_num,
            self.graph_add,
        )
        self.D2_PNA = Discriminator2(
            self.deg,
            self.agg,
            self.sca,
            self.pna_in_ch,
            self.pna_out_ch,
            self.edge_dim,
            self.towers,
            self.pre_lay,
            self.post_lay,
            self.pna_layer_num,
            self.graph_add,
        )
        self.PNA = PNA_Net(self.deg)
        self.D2_TraConv = Discriminator3(self.dim)
        self.V_PNA = Discriminator(
            self.deg,
            self.agg,
            self.sca,
            self.pna_in_ch,
            self.pna_out_ch,
            self.edge_dim,
            self.towers,
            self.pre_lay,
            self.post_lay,
            self.pna_layer_num,
            self.graph_add,
        )
        self.V2_PNA = Discriminator2(
            self.deg,
            self.agg,
            self.sca,
            self.pna_in_ch,
            self.pna_out_ch,
            self.edge_dim,
            self.towers,
            self.pre_lay,
            self.post_lay,
            self.pna_layer_num,
            self.graph_add,
        )

        """ Discriminator implementation with Graph Convolution:
            
            @ d_conv_dim: convolution dimensions for GCN
            @ m_dim: number of atom types (or number of features used)
            @ b_dim: number of bond types
            @ dropout: dropout possibility 
            """

        self.D = Discriminator_old(
            self.d_conv_dim, self.m_dim, self.b_dim, self.dropout, self.gcn_depth
        )
        self.D2 = Discriminator_old2(
            self.d_conv_dim,
            self.drugs_m_dim,
            self.drugs_b_dim,
            self.dropout,
            self.gcn_depth,
        )

        self.V = Discriminator_old(
            self.d_conv_dim, self.m_dim, self.b_dim, self.dropout, self.gcn_depth
        )
        self.V2 = Discriminator_old2(
            self.d_conv_dim,
            self.drugs_m_dim,
            self.drugs_b_dim,
            self.dropout,
            self.gcn_depth,
        )

        """ Optimizers for G1, G2, D1, and D2:
            
            Adam Optimizer is used and different beta1 and beta2s are used for GAN1 and GAN2
            """

        self.g_optimizer = torch.optim.AdamW(
            self.G.parameters(), self.g_lr, [self.beta1, self.beta2]
        )
        self.g2_optimizer = torch.optim.AdamW(
            self.G2.parameters(), self.g2_lr, [self.beta1, self.beta2]
        )

        self.d_optimizer = torch.optim.AdamW(
            self.D.parameters(), self.d_lr, [self.beta1, self.beta2]
        )
        self.d2_optimizer = torch.optim.AdamW(
            self.D2.parameters(), self.d2_lr, [self.beta1, self.beta2]
        )

        self.d_pna_optimizer = torch.optim.AdamW(
            self.PNA.parameters(), self.d_lr, [self.beta1, self.beta2]
        )
        self.d2_pna_optimizer = torch.optim.AdamW(
            self.D2_PNA.parameters(), self.d2_lr, [self.beta1, self.beta2]
        )

        self.d_traconv_optimizer = torch.optim.AdamW(
            self.D_TraConv.parameters(), self.d_lr, [self.beta1, self.beta2]
        )
        self.d2_traconv_optimizer = torch.optim.AdamW(
            self.D2_TraConv.parameters(), self.d2_lr, [self.beta1, self.beta2]
        )

        self.v_optimizer = torch.optim.AdamW(
            self.V.parameters(), self.d_lr, [self.beta1, self.beta2]
        )
        self.v2_optimizer = torch.optim.AdamW(
            self.V2.parameters(), self.d2_lr, [self.beta1, self.beta2]
        )
        """ Learning rate scheduler:
            
            Changes learning rate based on loss.
            """

        self.scheduler_g = ReduceLROnPlateau(
            self.g_optimizer, mode="min", factor=0.5, patience=10, min_lr=0.00001
        )

        self.scheduler_d = ReduceLROnPlateau(
            self.d_optimizer, mode="min", factor=0.5, patience=10, min_lr=0.00001
        )
        self.scheduler_d_pna = ReduceLROnPlateau(
            self.d_pna_optimizer, mode="min", factor=0.5, patience=10, min_lr=0.00001
        )
        self.scheduler_d_traconv = ReduceLROnPlateau(
            self.d_traconv_optimizer,
            mode="min",
            factor=0.5,
            patience=10,
            min_lr=0.00001,
        )
        self.scheduler_v = ReduceLROnPlateau(
            self.v_optimizer, mode="min", factor=0.5, patience=10, min_lr=0.00001
        )
        self.scheduler_g2 = ReduceLROnPlateau(
            self.g2_optimizer, mode="min", factor=0.5, patience=10, min_lr=0.00001
        )
        self.scheduler_d2 = ReduceLROnPlateau(
            self.d2_optimizer, mode="min", factor=0.5, patience=10, min_lr=0.00001
        )
        self.scheduler_d2_pna = ReduceLROnPlateau(
            self.d2_pna_optimizer, mode="min", factor=0.5, patience=10, min_lr=0.00001
        )
        self.scheduler_d2_traconv = ReduceLROnPlateau(
            self.d2_traconv_optimizer,
            mode="min",
            factor=0.5,
            patience=10,
            min_lr=0.00001,
        )
        self.scheduler_v2 = ReduceLROnPlateau(
            self.v2_optimizer, mode="min", factor=0.5, patience=10, min_lr=0.00001
        )
        self.print_network(self.G, "G")
        self.print_network(self.D, "D")

        self.print_network(self.G2, "G2")
        self.print_network(self.D2, "D2")

        self.G.to(self.device)
        self.D.to(self.device)
        self.PNA.to(self.device)
        self.D_TraConv.to(self.device)
        self.V.to(self.device)

        self.G2.to(self.device)
        self.D2.to(self.device)
        self.D2_PNA.to(self.device)
        self.D2_TraConv.to(self.device)
        self.V2.to(self.device)

        for p in self.G.parameters():
            if p.dim() > 1:
                if self.init_type == "uniform":
                    torch.nn.init.xavier_uniform_(p)
                elif self.init_type == "normal":
                    torch.nn.init.xavier_normal_(p)
                elif self.init_type == "random_normal":
                    torch.nn.init.normal_(p, 0.0, 0.02)
        for p in self.G2.parameters():
            if p.dim() > 1:
                if self.init_type == "uniform":
                    torch.nn.init.xavier_uniform_(p)
                elif self.init_type == "normal":
                    torch.nn.init.xavier_normal_(p)
                elif self.init_type == "random_normal":
                    torch.nn.init.normal_(p, 0.0, 0.02)
        if self.dis_select == "conv":
            for p in self.D.parameters():
                if p.dim() > 1:
                    if self.init_type == "uniform":
                        torch.nn.init.xavier_uniform_(p)
                    elif self.init_type == "normal":
                        torch.nn.init.xavier_normal_(p)
                    elif self.init_type == "random_normal":
                        torch.nn.init.normal_(p, 0.0, 0.02)
        elif self.dis_select == "PNA":
            for p in self.D_PNA.parameters():
                if p.dim() > 1:
                    if self.init_type == "uniform":
                        torch.nn.init.xavier_uniform_(p)
                    elif self.init_type == "normal":
                        torch.nn.init.xavier_normal_(p)
                    elif self.init_type == "random_normal":
                        torch.nn.init.normal_(p, 0.0, 0.02)
        if self.dis_select == "conv":
            for p in self.D2.parameters():
                if p.dim() > 1:
                    if self.init_type == "uniform":
                        torch.nn.init.xavier_uniform_(p)
                    elif self.init_type == "normal":
                        torch.nn.init.xavier_normal_(p)
                    elif self.init_type == "random_normal":
                        torch.nn.init.normal_(p, 0.0, 0.02)
        elif self.dis_select == "PNA":
            for p in self.D2_PNA.parameters():
                if p.dim() > 1:
                    if self.init_type == "uniform":
                        torch.nn.init.xavier_uniform_(p)
                    elif self.init_type == "normal":
                        torch.nn.init.xavier_normal_(p)
                    elif self.init_type == "random_normal":
                        torch.nn.init.normal_(p, 0.0, 0.02)

    def decoder_load(self, dictionary_name):
        """Loading the atom and bond decoders"""

        with open(
            "DrugDesigner/data/" + dictionary_name + self.dataset_name + ".pkl", "rb"
        ) as f:

            return pickle.load(f)

    def drug_decoder_load(self, dictionary_name):
        """Loading the atom and bond decoders"""

        with open("DrugDesigner/data/" + dictionary_name + ".pkl", "rb") as f:

            return pickle.load(f)

    def _genDegree(self):
        """Generates the Degree distribution tensor for PNA, should be used everytime a different
        dataset is used.
        Can be called without arguments and saves the tensor for later use. If tensor was created
        before, it just loads the degree tensor.
        """

        degree_path = os.path.join(self.degree_dir, self.dataset_name + "-degree.pt")
        if not os.path.exists(degree_path):

            max_degree = -1
            for data in self.dataset:
                d = geoutils.degree(
                    data.edge_index[1], num_nodes=data.num_nodes, dtype=torch.long
                )
                max_degree = max(max_degree, int(d.max()))

            # Compute the in-degree histogram tensor
            deg = torch.zeros(max_degree + 1, dtype=torch.long)
            for data in self.dataset:
                d = geoutils.degree(
                    data.edge_index[1], num_nodes=data.num_nodes, dtype=torch.long
                )
                deg += torch.bincount(d, minlength=deg.numel())
            torch.save(deg, "DrugDesigner/data/" + self.dataset_name + "-degree.pt")
        else:
            deg = torch.load(degree_path, map_location=lambda storage, loc: storage)

        return deg

    def print_network(self, model, name):
        """Print out the network information."""

        num_params = 0
        for p in model.parameters():
            num_params += p.numel()
        print(model)
        print(name)
        print("The number of parameters: {}".format(num_params))

    def restore_model(self, resume_iters):
        """Restore the trained generator and discriminator."""

        print("Loading the trained models from step {}...".format(resume_iters))

        G_path = os.path.join(self.model_directory, "{}-G.ckpt".format(resume_iters))
        D_path = os.path.join(self.model_directory, "{}-D.ckpt".format(resume_iters))
        V_path = os.path.join(self.model_directory, "{}-V.ckpt".format(resume_iters))

        self.G.load_state_dict(
            torch.load(G_path, map_location=lambda storage, loc: storage)
        )
        self.D.load_state_dict(
            torch.load(D_path, map_location=lambda storage, loc: storage)
        )
        self.V.load_state_dict(
            torch.load(V_path, map_location=lambda storage, loc: storage)
        )

        G2_path = os.path.join(self.model_directory, "{}-G2.ckpt".format(resume_iters))
        D2_path = os.path.join(self.model_directory, "{}-D2.ckpt".format(resume_iters))
        V2_path = os.path.join(self.model_directory, "{}-V2.ckpt".format(resume_iters))

        self.G2.load_state_dict(
            torch.load(G2_path, map_location=lambda storage, loc: storage)
        )
        self.D2.load_state_dict(
            torch.load(D2_path, map_location=lambda storage, loc: storage)
        )
        self.V2.load_state_dict(
            torch.load(V2_path, map_location=lambda storage, loc: storage)
        )

    def update_lr(self, g_lr, d_lr, g2_lr, d2_lr):
        """Decay learning rates of the generator and discriminator."""

        for param_group in self.g_optimizer.param_groups:
            param_group["lr"] = g_lr
        for param_group in self.g2_optimizer.param_groups:
            param_group["lr"] = g2_lr
        if self.dis_select == "conv":
            for param_group in self.d_optimizer.param_groups:
                param_group["lr"] = d_lr
            for param_group in self.d2_optimizer.param_groups:
                param_group["lr"] = d2_lr
        elif self.dis_select == "PNA":
            for param_group in self.d_pna_optimizer.param_groups:
                param_group["lr"] = d_lr
            for param_group in self.d2_pna_optimizer.param_groups:
                param_group["lr"] = d2_lr
        elif self.dis_select == "TraConv":
            for param_group in self.d_traconv_optimizer.param_groups:
                param_group["lr"] = d_lr
            for param_group in self.d2_traconv_optimizer.param_groups:
                param_group["lr"] = d2_lr

    def reset_grad(self):
        """Reset the gradient buffers."""

        self.g_optimizer.zero_grad()
        self.g2_optimizer.zero_grad()

        self.d_optimizer.zero_grad()
        self.d2_optimizer.zero_grad()

        if self.dis_select == "PNA":
            self.d_pna_optimizer.zero_grad()
            self.d2_pna_optimizer.zero_grad()
        elif self.dis_select == "TraConv":
            self.d_traconv_optimizer.zero_grad()
            self.d2_traconv_optimizer.zero_grad()

    def denorm(self, x):
        """Convert the range from [-1, 1] to [0, 1]."""

        out = (x + 1) / 2

        return out.clamp_(0, 1)

    def gradient_penalty(self, y, x):
        """Compute gradient penalty: (L2_norm(dy/dx) - 1)**2."""

        weight = torch.ones(y.size(), requires_grad=False).to(self.device)
        dydx = torch.autograd.grad(
            outputs=y,
            inputs=x,
            grad_outputs=weight,
            retain_graph=True,
            create_graph=True,
            only_inputs=True,
        )[0]

        dydx = dydx.view(dydx.size(0), -1)
        gradient_penalty = ((dydx.norm(2, dim=1) - 1) ** 2).mean()

        return gradient_penalty

    def label2onehot(self, labels, dim):
        """Convert label indices to one-hot vectors."""

        out = torch.zeros(list(labels.size()) + [dim]).to(self.device)
        out.scatter_(len(out.size()) - 1, labels.unsqueeze(-1), 1.0)

        return out.float()

    def sample_z_node(self, batch_size):
        """Random noise for nodes logits."""

        return np.random.normal(
            0, 1, size=(batch_size, self.vertexes, self.m_dim)
        )  #  128, 9, 5

    def sample_z_edge(self, batch_size):
        """Random noise for edges logits."""

        return np.random.normal(
            0, 1, size=(batch_size, self.vertexes, self.vertexes, self.b_dim)
        )  # 128, 9, 9, 5

    def postprocess(self, inputs, post_method, temperature=1.0, dimension=-1):

        if post_method == "soft_gumbel":
            softmax = F.gumbel_softmax(inputs / temperature, hard=False, dim=dimension)
        elif post_method == "hard_gumbel":
            softmax = F.gumbel_softmax(inputs / temperature, hard=True, dim=dimension)
        elif post_method == "softmax":
            softmax = F.softmax(inputs / temperature, dim=dimension)

        return softmax

    def reward(self, mols):
        """Rewards that can be used for Reinforcement Networks."""

        rr = 1.0
        for m in (
            "logp,sas,qed,unique" if self.metrics == "all" else self.metrics
        ).split(","):

            if m == "np":
                rr *= MolecularMetrics.natural_product_scores(mols, norm=True)
            elif m == "logp":
                rr *= MolecularMetrics.water_octanol_partition_coefficient_scores(
                    mols, norm=True
                )
            elif m == "sas":
                rr *= MolecularMetrics.synthetic_accessibility_score_scores(
                    mols, norm=True
                )
            elif m == "qed":
                rr *= MolecularMetrics.quantitative_estimation_druglikeness_scores(
                    mols, norm=True
                )
            elif m == "novelty":
                rr *= MolecularMetrics.novel_scores(mols, self.data)
            elif m == "dc":
                rr *= MolecularMetrics.drugcandidate_scores(mols, self.data)
            elif m == "unique":
                rr *= MolecularMetrics.unique_scores(mols)
            elif m == "diversity":
                rr *= MolecularMetrics.diversity_scores(mols, self.data)
            elif m == "validity":
                rr *= MolecularMetrics.valid_total_score(mols)
            else:
                raise RuntimeError("{} is not defined as a metric".format(m))

        return rr.reshape(-1, 1)

    def dense_to_sparse_with_attr(self, adj):
        ###
        assert adj.dim() >= 2 and adj.dim() <= 3
        assert adj.size(-1) == adj.size(-2)

        index = adj.nonzero(as_tuple=True)
        edge_attr = adj[index]

        if len(index) == 3:
            batch = index[0] * adj.size(-1)
            index = (batch + index[1], batch + index[2])
            index = torch.stack(index, dim=0)
        return index, edge_attr

    def plot_grad_flow(self, named_parameters, model, iter, epoch):

        # Based on https://discuss.pytorch.org/t/check-gradient-flow-in-network/15063/10
        """Plots the gradients flowing through different layers in the net during training.
        Can be used for checking for possible gradient vanishing / exploding problems.

        Usage: Plug this function in Trainer class after loss.backwards() as
        "plot_grad_flow(self.model.named_parameters())" to visualize the gradient flow
        """
        ave_grads = []
        max_grads = []
        layers = []
        for n, p in named_parameters:
            if (p.requires_grad) and ("bias" not in n):
                print(p.grad, n)
                layers.append(n)
                ave_grads.append(p.grad.abs().mean().cpu())
                max_grads.append(p.grad.abs().max().cpu())
        plt.bar(np.arange(len(max_grads)), max_grads, alpha=0.1, lw=1, color="c")
        plt.bar(np.arange(len(max_grads)), ave_grads, alpha=0.1, lw=1, color="b")
        plt.hlines(0, 0, len(ave_grads) + 1, lw=2, color="k")
        plt.xticks(range(0, len(ave_grads), 1), layers, rotation="vertical")
        plt.xlim(left=0, right=len(ave_grads))
        plt.ylim(bottom=-0.001, top=1)  # zoom in on the lower gradient regions
        plt.xlabel("Layers")
        plt.ylabel("average gradient")
        plt.title("Gradient flow")
        plt.grid(True)
        plt.legend(
            [
                Line2D([0], [0], color="c", lw=4),
                Line2D([0], [0], color="b", lw=4),
                Line2D([0], [0], color="k", lw=4),
            ],
            ["max-gradient", "mean-gradient", "zero-gradient"],
        )
        pltsavedir = "/home/atabey/gradients/tryout"
        plt.savefig(
            os.path.join(
                pltsavedir,
                "weights_"
                + model
                + "_"
                + self.dataset_name
                + "_"
                + str(iter)
                + "_"
                + str(epoch)
                + ".png",
            ),
            dpi=500,
            bbox_inches="tight",
        )

    def plot_attn(self, attn_w, model, iter, epoch):

        cols = 4
        rows = int(self.heads / cols)

        fig, axes = plt.subplots(rows, cols, figsize=(30, 14))
        axes = axes.flat
        attentions_pos = attn_w[0]
        attentions_pos = attentions_pos.cpu().detach().numpy()
        for i, att in enumerate(attentions_pos):

            # im = axes[i].imshow(att, cmap='gray')
            sns.heatmap(att, vmin=0, vmax=1, ax=axes[i])
            axes[i].set_title(f"head - {i} ")
            axes[i].set_ylabel("layers")
        pltsavedir = "/home/atabey/attn/second"
        plt.savefig(
            os.path.join(
                pltsavedir,
                "attn"
                + model
                + "_"
                + self.dataset_name
                + "_"
                + str(iter)
                + "_"
                + str(epoch)
                + ".png",
            ),
            dpi=500,
            bbox_inches="tight",
        )

    def train(self):
        """Training Script starts from here"""

        wandb.config = {"beta1": 0.9}
        wandb.init(project="drugdesigner", entity="atabeyunlu")

        # Defining sampling paths and creating logger
        bce_loss = torch.nn.BCELoss()
        self.arguments = "alt_loss__glr{}_dlr{}_g2lr{}_d2lr{}_dim{}_depth{}_heads{}_decdepth{}_decheads{}_ncritic{}_batch{}_epoch{}_warmup{}_dataset{}_disc-{}_la{}_dropout{}".format(
            self.g_lr,
            self.d_lr,
            self.g2_lr,
            self.d2_lr,
            self.dim,
            self.depth,
            self.heads,
            self.dec_depth,
            self.dec_heads,
            self.n_critic,
            self.batch_size,
            self.num_iters,
            self.warm_up_steps,
            self.dataset_name,
            self.dis_select,
            self.la,
            self.dropout,
        )
        writer = SummaryWriter(log_dir=os.path.join(self.result_dir, self.arguments))
        self.model_directory = os.path.join(self.model_save_dir, self.arguments)
        self.sample_directory = os.path.join(self.sample_dir, self.arguments)
        log_path = os.path.join(self.log_dir, "{}.txt".format(self.arguments))
        if not os.path.exists(self.model_directory):
            os.makedirs(self.model_directory)
        if not os.path.exists(self.sample_directory):
            os.makedirs(self.sample_directory)

        # Learning rate cache for decaying.

        g_lr = self.g_lr
        d_lr = self.d_lr
        g2_lr = self.g2_lr
        d2_lr = self.d2_lr

        # protein data

        akt1_human_adj = torch.load("DrugDesigner/data/akt/AKT1_human_adj.pt")
        akt1_human_annot = torch.load("DrugDesigner/data/akt/AKT1_human_annot.pt")

        # Start training.

        print("Start training...")
        start_time = time.time()
        for idx in range(self.num_iters):

            # =================================================================================== #
            #                             1. Preprocess input data                                #
            # =================================================================================== #

            # Load the data

            dataloader_iterator = iter(self.drugs_loader)

            for i, data in enumerate(self.loader):
                try:
                    drugs = next(dataloader_iterator)
                except StopIteration:
                    dataloader_iterator = iter(self.drugs_loader)
                    drugs = next(dataloader_iterator)

                # Preprocess both dataset

                data = data.to(self.device)
                drugs = drugs.to(self.device)
                z_e = self.sample_z_edge(self.batch_size)  # (batch,max_len,max_len)
                z_n = self.sample_z_node(self.batch_size)  # (batch,max_len)
                z_edge = (
                    torch.from_numpy(z_e).to(self.device).float().requires_grad_(True)
                )  # Edge noise.(batch,max_len,max_len)
                z_node = (
                    torch.from_numpy(z_n).to(self.device).float().requires_grad_(True)
                )  # Node noise.(batch,max_len)
                a = geoutils.to_dense_adj(
                    edge_index=data.edge_index,
                    batch=data.batch,
                    edge_attr=data.edge_attr,
                    max_num_nodes=int(data.batch.shape[0] / self.batch_size),
                )
                x = data.x.view(-1, int(data.batch.shape[0] / self.batch_size))

                a_tensor = self.label2onehot(a, self.b_dim)
                x_tensor = self.label2onehot(x, self.m_dim)
                a_tensor = (
                    a_tensor
                    + torch.randn(
                        [a_tensor.size(0), a_tensor.size(1), a_tensor.size(2), 1],
                        device=a_tensor.device,
                    )
                    * self.noise_strength_0
                )
                x_tensor = (
                    x_tensor
                    + torch.randn(
                        [x_tensor.size(0), x_tensor.size(1), 1], device=x_tensor.device
                    )
                    * self.noise_strength_1
                )

                drugs_a = geoutils.to_dense_adj(
                    edge_index=drugs.edge_index,
                    batch=drugs.batch,
                    edge_attr=drugs.edge_attr,
                    max_num_nodes=int(drugs.batch.shape[0] / self.batch_size),
                )
                drugs_x = drugs.x.view(-1, int(drugs.batch.shape[0] / self.batch_size))

                drugs_a = drugs_a.to(self.device).long()
                drugs_x = drugs_x.to(self.device).long()
                drugs_a_tensor = self.label2onehot(drugs_a, self.drugs_b_dim)
                drugs_x_tensor = self.label2onehot(drugs_x, self.drugs_m_dim)
                drugs_a_tensor = (
                    drugs_a_tensor
                    + torch.randn(
                        [
                            drugs_a_tensor.size(0),
                            drugs_a_tensor.size(1),
                            drugs_a_tensor.size(2),
                            1,
                        ],
                        device=drugs_a_tensor.device,
                    )
                    * self.noise_strength_2
                )
                drugs_x_tensor = (
                    drugs_x_tensor
                    + torch.randn(
                        [drugs_x_tensor.size(0), drugs_x_tensor.size(1), 1],
                        device=drugs_x_tensor.device,
                    )
                    * self.noise_strength_3
                )
                prot_n = akt1_human_annot[None, :].to(self.device).float()
                prot_e = (
                    akt1_human_adj[None, None, :]
                    .view(1, 546, 546, 1)
                    .to(self.device)
                    .float()
                )

                y_real = torch.autograd.Variable(torch.ones(self.batch_size, 1)).to(
                    self.device
                )
                y_fake = torch.autograd.Variable(torch.zeros(self.batch_size, 1)).to(
                    self.device
                )
                y_fake_value = torch.autograd.Variable(
                    torch.zeros(self.batch_size, 1)
                ).to(self.device)

                # =================================================================================== #
                #                             2. Train the discriminator                              #
                # =================================================================================== #
                self.reset_grad()
                # Compute loss with real molecules.

                if self.dis_select == "conv":

                    logits_real, features_real = self.D(a_tensor, None, x_tensor)

                elif self.dis_select == "PNA":

                    logits_real = self.PNA(
                        x_tensor.to(self.device).float(),
                        a.to(self.device).float(),
                        mask=None,
                    )

                prediction_real = -torch.mean(logits_real)

                # Compute loss with fake molecules.

                edges_logits, nodes_logits, attn = self.G(
                    z_edge, z_node, a, a_tensor, x_tensor
                )

                edges_hat = self.postprocess(edges_logits, "soft_gumbel")
                nodes_hat = self.postprocess(nodes_logits, "soft_gumbel")

                edges_hat_sample, nodes_hat_sample = (
                    torch.max(edges_hat, -1)[1],
                    torch.max(nodes_hat, -1)[1],
                )

                features_hat = None

                if self.dis_select == "conv":

                    logits_fake, features_fake = self.D(edges_hat, None, nodes_hat)

                elif self.dis_select == "PNA":

                    logits_fake = self.PNA(
                        nodes_hat, edges_hat_sample.float(), mask=None
                    )

                prediction_fake = torch.mean(logits_fake)
                eps = torch.rand(logits_real.size(0), 1, 1, 1).to(self.device)
                x_int0 = (eps * a_tensor + (1.0 - eps) * edges_hat).requires_grad_(True)
                x_int1 = (
                    eps.squeeze(-1) * x_tensor + (1.0 - eps.squeeze(-1)) * nodes_hat
                ).requires_grad_(True)

                grad0, grad1 = self.D(x_int0, None, x_int1, torch.sigmoid)
                d_loss_gp = self.gradient_penalty(
                    grad0, x_int0
                ) + self.gradient_penalty(grad1, x_int1)

                # Calculate total loss
                d_loss = prediction_fake + prediction_real + d_loss_gp * self.lambda_gp

                # Calculate total loss

                d_total = d_loss
                if (idx + 1 > self.warm_up_steps) & (i + 1 > 10):

                    # =================================================================================== #
                    #                             4. Train the discriminator - 2                          #
                    # =================================================================================== #

                    tra_edges_logits, tra_nodes_logits, dec_attn = self.G2(
                        edges_logits, nodes_logits, prot_n, prot_e
                    )

                    tra_edges_hat = self.postprocess(tra_edges_logits, "soft_gumbel")
                    tra_nodes_hat = self.postprocess(tra_nodes_logits, "soft_gumbel")

                    if self.dis_select == "conv":

                        logits_fake2, features_fake2 = self.D2(
                            tra_edges_hat, None, tra_nodes_hat
                        )

                    d2_loss_fake = torch.mean(logits_fake2)

                    if self.dis_select == "conv":

                        logits_real2, features_real2 = self.D2(
                            drugs_a_tensor, None, drugs_x_tensor
                        )

                    elif self.dis_select == "PNA":

                        logits_real2 = self.D2_PNA(
                            drugs.x, drugs.edge_index, drugs.edge_attr, drugs.batch
                        )

                    d2_loss_real = -torch.mean(logits_real2)
                    pad_adj_shape = drugs_a_tensor.shape[1] - tra_edges_hat.shape[1]
                    pad_x_features_shape = (
                        drugs_x_tensor.shape[-1] - tra_nodes_hat.shape[-1]
                    )

                    pad_adj = (0, 0, 0, pad_adj_shape, 0, pad_adj_shape)
                    pad_x = (0, pad_x_features_shape, 0, pad_adj_shape)

                    tra_edges_hats = F.pad(tra_edges_hat, pad_adj)
                    tra_nodes_hats = F.pad(tra_nodes_hat, pad_x)

                    eps = torch.rand(logits_real.size(0), 1, 1, 1).to(self.device)
                    x_int0 = (
                        eps * drugs_a_tensor + (1.0 - eps) * tra_edges_hats
                    ).requires_grad_(True)
                    x_int1 = (
                        eps.squeeze(-1) * drugs_x_tensor
                        + (1.0 - eps.squeeze(-1)) * tra_nodes_hats
                    ).requires_grad_(True)

                    grad0, grad1 = self.D2(x_int0, None, x_int1, torch.sigmoid)
                    d2_loss_gp = self.gradient_penalty(
                        grad0, x_int0
                    ) + self.gradient_penalty(grad1, x_int1)

                    d2_loss = d2_loss_fake + d2_loss_real + d2_loss_gp * self.lambda_gp

                    loss["D2/d2_loss_real"] = d2_loss_real.item()
                    loss["D2/d2_loss_fake"] = d2_loss_fake.item()

                    loss["D2/d2_loss"] = d2_loss.item()
                    wandb.log(
                        {
                            "d2_loss_fake": d2_loss_fake,
                            "d2_loss_real": d2_loss_real,
                            "d2_loss": d2_loss,
                            "iteration/epoch": [i, idx],
                        }
                    )

                    d_total = self.la * d_loss + (1 - self.la) * d2_loss

                # Feed the loss
                d_total.backward()

                if self.dis_select == "conv":
                    torch.nn.utils.clip_grad_norm_(
                        self.D.parameters(), self.clipping_value
                    )
                    torch.nn.utils.clip_grad_norm_(
                        self.D2.parameters(), self.clipping_value
                    )

                # if (i+1) % 100 == 0:
                #   if self.dis_select == "conv":
                #      self.plot_grad_flow(self.D.named_parameters(),"D",i+1,idx)

                if self.dis_select == "conv":

                    self.d_optimizer.step()
                    self.scheduler_d.step(d_total)
                    if idx + 1 > self.warm_up_steps:
                        self.d2_optimizer.step()
                        self.scheduler_d2.step(d_total)

                # Logging.

                loss = {}
                loss["D/d_loss_real"] = prediction_real.item()
                loss["D/d_loss_fake"] = prediction_fake.item()

                loss["D/d_loss"] = d_loss.item()
                loss["D_T/d_total"] = d_total.item()

                wandb.log(
                    {
                        "d_loss_fake": prediction_fake,
                        "d_loss_real": prediction_real,
                        "d_loss": d_loss,
                        "iteration/epoch": [i, idx],
                    }
                )

                wandb.log({"d_total": d_total, "iteration/epoch": [i, idx]})

                # =================================================================================== #
                #                            3. Train the generator                                   #
                # =================================================================================== #
                if (i + 1) % self.n_critic == 0:
                    self.reset_grad()
                    # Generate fake molecules.

                    g_edges_logits, g_nodes_logits, g_attn = self.G(
                        z_edge, z_node, a, a_tensor, x_tensor
                    )

                    # Postprocess with Gumbel softmax

                    # g_edges_hat = g_edges_logits.view(-1,self.b_dim,self.vertexes,self.vertexes)
                    g_edges_hat = self.postprocess(g_edges_logits, "soft_gumbel")
                    g_nodes_hat = self.postprocess(g_nodes_logits, "soft_gumbel")

                    g_edges_hat_sample, g_nodes_hat_sample = (
                        torch.max(g_edges_hat, -1)[1],
                        torch.max(g_nodes_hat, -1)[1],
                    )

                    features_hat = None
                    # g_fake_edge_index, g_fake_edge_attr = self.dense_to_sparse_with_attr(g_edges_hat)
                    g_nodes_fake = g_nodes_hat_sample.view(-1, 1)
                    if (i + 1) % (self.log_step) == 0:
                        fake_mol = [
                            self.dataset.matrices2mol(
                                n_.data.cpu().numpy(),
                                e_.data.cpu().numpy(),
                                strict=True,
                            )
                            for e_, n_ in zip(g_edges_hat_sample, g_nodes_hat_sample)
                        ]

                        mols = [
                            self.dataset.matrices2mol(
                                n_.data.cpu().numpy(),
                                e_.data.cpu().numpy(),
                                strict=False,
                            )
                            for e_, n_ in zip(a, x)
                        ]
                    # g_edges_hat = g_edges_hat.view(-1, self.vertexes,self.vertexes,self.b_dim)
                    # Compute loss with fake molecules.
                    if self.dis_select == "conv":

                        g_logits_fake, g_features_fake = self.D(
                            g_edges_hat, None, g_nodes_hat
                        )

                    elif self.dis_select == "PNA":

                        # g_logits_fake = self.PNA(g_nodes_fake, g_fake_edge_index, g_fake_edge_attr, data.batch)
                        g_logits_fake = self.PNA(
                            g_nodes_hat, edges_hat_sample.float(), mask=None
                        )
                    # elif self.dis_select == "TraConv":

                    # g_logits_fake = self.D_TraConv(g_nodes_for_traconv, g_fake_edge_index, g_attr_for_traconv, data.batch)

                    g_prediction_fake = -torch.mean(g_logits_fake)
                    # g_loss_fake = bce_loss(torch.sigmoid(g_logits_fake),y_real)

                    # Real Reward

                    # rewardR = torch.from_numpy(self.reward(mols)).to(self.device)

                    # Fake Reward

                    # rewardF = torch.from_numpy(self.reward(fake_mol)).to(self.device)

                    # Reinforcement Loss

                    # value_logit_real,_ = self.V(a_tensor, None, x_tensor,torch.sigmoid)
                    # value_logit_fake,_ = self.V(g_edges_hat, None, g_nodes_hat, torch.sigmoid)
                    # g_loss_value_pred =  torch.mean((value_logit_real - rewardR) ** 2 + (value_logit_fake - rewardF) ** 2)
                    # g_loss_value_pred =  (1 - rewardF) ** 2
                    # g_loss_value = bce_loss(torch.sigmoid(g_loss_value_pred),y_fake_value)

                    # Clone edge and node logits for GAN2

                    # Backward and optimize.

                    g_loss = g_prediction_fake  # + (1. - self.la) * g_loss_value_pred

                    # self.scheduler_v.step(g_loss)

                    # Logging.

                    loss["G/g_loss_fake"] = g_prediction_fake.item()
                    # loss['G/g_loss_value'] = g_loss_value_pred.item()

                    loss["G/g_loss"] = g_loss.item()
                    wandb.log({"g_loss": g_loss, "iteration/epoch": [i, idx]})

                    # =================================================================================== #
                    #                             5. Train the generator - 2                              #
                    # =================================================================================== #
                    g_total = g_loss
                    if (idx + 1 > self.warm_up_steps) & ((i + 1) % self.n_critic == 0):

                        g_tra_edges_logits, g_tra_nodes_logits, g_dec_attn = self.G2(
                            g_edges_logits, g_nodes_logits, prot_n, prot_e
                        )

                        g_tra_edges_hat = self.postprocess(
                            g_tra_edges_logits, "soft_gumbel"
                        )
                        g_tra_nodes_hat = self.postprocess(
                            g_tra_nodes_logits, "soft_gumbel"
                        )

                        g_tra_edges_hard, g_tra_nodes_hard = (
                            torch.max(g_tra_edges_hat, -1)[1],
                            torch.max(g_tra_nodes_hat, -1)[1],
                        )

                        if self.dis_select == "conv":

                            g_tra_logits_fake2, g_tra_features_fake2 = self.D2(
                                g_tra_edges_hat, None, g_tra_nodes_hat
                            )

                        g2_loss_fake = -torch.mean(g_tra_logits_fake2)
                        if (i + 1) % (self.log_step) == 0:
                            fake_mol2 = [
                                self.dataset.matrices2mol_drugs(
                                    n_.data.cpu().numpy(),
                                    e_.data.cpu().numpy(),
                                    strict=True,
                                )
                                for e_, n_ in zip(g_tra_edges_hard, g_tra_nodes_hard)
                            ]

                            drugs_mol = [
                                self.dataset.matrices2mol_drugs(
                                    n_.data.cpu().numpy(),
                                    e_.data.cpu().numpy(),
                                    strict=True,
                                )
                                for e_, n_ in zip(drugs_a, drugs_x)
                            ]

                        # rewardR2 = torch.from_numpy(self.reward(drugs_mol)).to(self.device)

                        # rewardF2 = torch.from_numpy(self.reward(fake_mol2)).to(self.device)

                        g2_loss = g2_loss_fake
                        loss["G2/g2_loss_fake"] = g2_loss_fake.item()
                        loss["G2/g2_loss"] = g2_loss.item()
                        wandb.log({"g2_loss": g2_loss, "iteration/epoch": [i, idx]})
                        g_total = self.la2 * g_loss + (1 - self.la2) * g2_loss

                    g_total.backward()
                    torch.nn.utils.clip_grad_norm_(
                        self.G.parameters(), self.clipping_value
                    )
                    torch.nn.utils.clip_grad_norm_(
                        self.G2.parameters(), self.clipping_value
                    )

                    # if (i+1) % 100 == 0:
                    #   self.plot_grad_flow(self.G.named_parameters(),"G",i+1,idx)
                    #  print(g_edges_hat_sample[0], g_nodes_hat_sample[0])

                    self.g_optimizer.step()
                    self.scheduler_g.step(g_total)
                    if idx + 1 > self.warm_up_steps:
                        self.g2_optimizer.step()
                        self.scheduler_g2.step(g_total)

                    loss["G_T/g_total"] = g_total.item()

                    wandb.log({"g_total": g_total, "iteration/epoch": [i, idx]})
                    # =================================================================================== #
                    #                                 6. Miscellaneous                                    #
                    # =================================================================================== #

                # Print out training information.
                if (i + 1) % (self.log_step) == 0:

                    et = time.time() - start_time
                    et = str(datetime.timedelta(seconds=et))[:-7]
                    log = "Elapsed [{}], Epoch/Iteration [{}/{}] for GAN1".format(
                        et, idx + 1, i + 1
                    )

                    # Log update
                    m0, m1 = all_scores(
                        fake_mol, mols, self.vertexes, norm=True
                    )  # 'mols' is output of Fake Reward
                    m0 = {k: np.array(v)[np.nonzero(v)].mean() for k, v in m0.items()}
                    m0.update(m1)
                    m2, m3 = all_scores(fake_mol2, mols, self.vertexes, norm=True)
                    loss.update(m0)
                    m2.update(m3)
                    loss.update(m2)
                    wandb.log({"metrics,": m1})
                    for tag, value in loss.items():

                        log += ", {}: {:.4f}".format(tag, value)

                    print(log)

                # Save model checkpoints.
                if (i + 1) % self.model_save_step == 0:
                    G_path = os.path.join(
                        self.model_directory, "{}_{}-G.ckpt".format(idx + 1, i + 1)
                    )
                    D_path = os.path.join(
                        self.model_directory, "{}_{}-D.ckpt".format(idx + 1, i + 1)
                    )
                    V_path = os.path.join(
                        self.model_directory, "{}_{}-V.ckpt".format(idx + 1, i + 1)
                    )

                    torch.save(self.G.state_dict(), G_path)
                    torch.save(self.D.state_dict(), D_path)
                    torch.save(self.V.state_dict(), V_path)
                    if idx + 1 >= self.warm_up_steps:
                        G2_path = os.path.join(
                            self.model_directory, "{}_{}-G2.ckpt".format(idx + 1, i + 1)
                        )
                        D2_path = os.path.join(
                            self.model_directory, "{}_{}-D2.ckpt".format(idx + 1, i + 1)
                        )
                        V2_path = os.path.join(
                            self.model_directory, "{}_{}-V2.ckpt".format(idx + 1, i + 1)
                        )

                        torch.save(self.G2.state_dict(), G2_path)
                        torch.save(self.D2.state_dict(), D2_path)
                        torch.save(self.V2.state_dict(), V2_path)

                    print(
                        "Saved model checkpoints into {}...".format(self.model_save_dir)
                    )

                # Decay learning rates.
                if (i + 1) % self.lr_update_step == 0 and (i + 1) > (
                    self.num_iters - self.num_iters_decay
                ):
                    g_lr -= self.g_lr / float(self.num_iters_decay)
                    d_lr -= self.d_lr / float(self.num_iters_decay)
                    g2_lr -= self.g2_lr / float(self.num_iters_decay)
                    d2_lr -= self.d2_lr / float(self.num_iters_decay)
                    self.update_lr(g_lr, d_lr, g2_lr, d2_lr)
                    print(
                        "Decayed learning rates, g_lr: {}, d_lr: {}, g2_lr: {}, d2_lr: {}.".format(
                            g_lr, d_lr, g2_lr, d2_lr
                        )
                    )

                if (i + 1) % (self.sample_step) == 0:
                    self.sample_path = os.path.join(
                        self.sample_directory,
                        "{}_{}-epoch_iteration".format(idx + 1, i + 1),
                    )
                    self.sample_path_GAN2 = os.path.join(
                        self.sample_directory,
                        "GAN2-{}_{}-epoch_iteration".format(idx + 1, i + 1),
                    )
                    if not os.path.exists(self.sample_path):
                        os.makedirs(self.sample_path)
                    mols2grid_image(fake_mol, self.sample_path)
                    save_smiles_matrices(
                        fake_mol,
                        g_edges_hat_sample.detach(),
                        g_nodes_hat_sample.detach(),
                        self.sample_path,
                    )
                    if len(os.listdir(self.sample_path)) == 0:
                        os.rmdir(self.sample_path)
                    if idx + 1 > self.warm_up_steps:
                        if not os.path.exists(self.sample_path_GAN2):
                            os.makedirs(self.sample_path_GAN2)
                        mols2grid_image(fake_mol2, self.sample_path_GAN2)
                        save_smiles_matrices(
                            fake_mol2,
                            g_tra_edges_hard.detach(),
                            g_tra_nodes_hard.detach(),
                            self.sample_path_GAN2,
                        )
                        attn_path = os.path.join(
                            "DrugDesigner/data/attn_tensors",
                            "{}_{}-attn_GAN2.pt".format(idx + 1, i + 1),
                        )
                        if len(os.listdir(self.sample_path_GAN2)) == 0:
                            os.rmdir(self.sample_path_GAN2)
                        # torch.save(attn, attn_path)
                    print("Valid molecules are saved.")
                    print("Valid matrices and smiles are saved")

                if (i + 1) % self.n_critic == 0:

                    writer.add_scalar("D/loss_real", prediction_real.item(), i)
                    writer.add_scalar("D/loss_fake", prediction_fake.item(), i)
                    writer.add_scalar("D/loss", d_loss.item(), i)
                    writer.add_scalar("D_T/d_total", d_total.item(), i)
                    writer.add_scalar("G/g_loss", g_loss.item(), i)
                    writer.add_scalar("G_T/g_total", g_total.item(), i)
                    if (idx + 1 > self.warm_up_steps) & (i + 1 > 10):
                        writer.add_scalar("D2/d2_loss_real", d2_loss_real.item(), i)
                        writer.add_scalar("D2/d2_loss_fake", d2_loss_fake.item(), i)
                        writer.add_scalar("D2/d2_loss", d2_loss.item(), i)
                        writer.add_scalar("G2/g2_loss", g2_loss.item(), i)

            writer.close()

    def test(self):
        # Load the trained generator.
        self.restore_model(self.test_iters)
        akt1_human_adj = torch.load("DrugDesigner/data/akt/AKT1_human_adj.pt")
        akt1_human_annot = torch.load("DrugDesigner/data/akt/AKT1_human_annot.pt")
        prot_n = akt1_human_annot[None, :].to(self.device)
        prot_e = (
            akt1_human_adj[None, None, :].view(1, 546, 546, 1).to(self.device).long()
        )
        date = time.time()
        with torch.no_grad():
            for idx in range(self.num_test_epoch):

                # =================================================================================== #
                #                             1. Preprocess input data                                #
                # =================================================================================== #

                # Load the data

                dataloader_iterator = iter(self.drugs_loader)

                for i, data in enumerate(self.loader):
                    try:
                        drugs = next(dataloader_iterator)
                    except StopIteration:
                        dataloader_iterator = iter(self.drugs_loader)
                        drugs = next(dataloader_iterator)

                    data = data.to(self.device)

                    drugs = drugs.to(self.device)
                    z_e = self.sample_z_edge(self.batch_size)  # (batch,max_len,max_len)
                    z_n = self.sample_z_node(self.batch_size)  # (batch,max_len)
                    z_edge = (
                        torch.from_numpy(z_e).to(self.device).float()
                    )  # Edge noise.(batch,max_len,max_len)
                    z_node = (
                        torch.from_numpy(z_n).to(self.device).float()
                    )  # Node noise.(batch,max_len)
                    a = geoutils.to_dense_adj(
                        edge_index=data.edge_index,
                        batch=data.batch,
                        edge_attr=data.edge_attr,
                        max_num_nodes=int(data.batch.shape[0] / self.batch_size),
                    )
                    x = data.x.view(-1, int(data.batch.shape[0] / self.batch_size))

                    a_tensor = self.label2onehot(a, self.b_dim)
                    x_tensor = self.label2onehot(x, self.m_dim)

                    drugs_a = geoutils.to_dense_adj(
                        edge_index=drugs.edge_index,
                        batch=drugs.batch,
                        edge_attr=drugs.edge_attr,
                        max_num_nodes=int(drugs.batch.shape[0] / self.batch_size),
                    )
                    drugs_x = drugs.x.view(
                        -1, int(drugs.batch.shape[0] / self.batch_size)
                    )

                    drugs_a = drugs_a.to(self.device).long()
                    drugs_x = drugs_x.to(self.device).long()
                    drugs_a_tensor = self.label2onehot(drugs_a, self.drugs_b_dim)
                    drugs_x_tensor = self.label2onehot(drugs_x, self.drugs_m_dim)
                    drugs_a_tensor = (
                        drugs_a_tensor
                        + torch.randn(
                            [
                                drugs_a_tensor.size(0),
                                drugs_a_tensor.size(1),
                                drugs_a_tensor.size(2),
                                1,
                            ],
                            device=drugs_a_tensor.device,
                        )
                        * self.noise_strength_2
                    )
                    drugs_x_tensor = (
                        drugs_x_tensor
                        + torch.randn(
                            [drugs_x_tensor.size(0), drugs_x_tensor.size(1), 1],
                            device=drugs_x_tensor.device,
                        )
                        * self.noise_strength_3
                    )
                    real_edge_attr_for_traconv = self.attr_mlp(
                        data.edge_attr.view(-1, 1).float()
                    )
                    real_nodes_for_traconv = self.nodes_mlp(data.x.view(-1, 1).float())
                    mols = [
                        self.dataset.matrices2mol(
                            n_.data.cpu().numpy(), e_.data.cpu().numpy(), strict=False
                        )
                        for e_, n_ in zip(a, x)
                    ]

                    # =================================================================================== #
                    #                             2. GAN1 Inference                                       #
                    # =================================================================================== #
                    (
                        edges_hat,
                        nodes_hat,
                        edges_logits,
                        nodes_logits,
                        nodes_fake,
                        fake_edge_index,
                        fake_edge_attr,
                        attr_for_traconv,
                        nodes_for_traconv,
                    ) = self.G(z_edge, z_node, a, a_tensor, x_tensor)
                    edges_hat = edges_hat.view(
                        -1, self.vertexes, self.vertexes, self.b_dim
                    )
                    if self.dis_select == "conv":

                        logits_real, features_real = self.D(a_tensor, None, x_tensor)

                    elif self.dis_select == "PNA":

                        logits_real = self.D_PNA(
                            data.x, data.edge_index, data.edge_attr, data.batch
                        )

                    elif self.dis_select == "TraConv":

                        logits_real = self.D_TraConv(
                            real_nodes_for_traconv,
                            data.edge_index,
                            real_edge_attr_for_traconv,
                            data.batch,
                        )

                    d_loss_real = -torch.mean(logits_real)

                    if self.dis_select == "conv":

                        logits_fake, features_fake = self.D(edges_hat, None, nodes_hat)

                    elif self.dis_select == "PNA":

                        logits_fake = self.D_PNA(
                            nodes_fake, fake_edge_index, fake_edge_attr, data.batch
                        )

                    elif self.dis_select == "TraConv":

                        logits_fake = self.D_TraConv(
                            nodes_for_traconv,
                            fake_edge_index,
                            attr_for_traconv,
                            data.batch,
                        )
                    g_edges_hard, g_nodes_hard = (
                        torch.max(edges_hat, 1)[1],
                        torch.max(nodes_hat, -1)[1],
                    )
                    fake_mol = [
                        self.dataset.matrices2mol(
                            n_.data.cpu().numpy(), e_.data.cpu().numpy(), strict=True
                        )
                        for e_, n_ in zip(g_edges_hard, g_nodes_hard)
                    ]

                    d_loss_fake = torch.mean(logits_fake)
                    g_loss = -torch.mean(logits_fake)
                    # =================================================================================== #
                    #                             3. GAN2 Inference                                       #
                    # =================================================================================== #
                    edges_logits_forGAN2, nodes_logits_forGAN2 = (
                        edges_logits.detach().clone(),
                        nodes_logits.detach().clone(),
                    )
                    (
                        tra_edges_hat,
                        tra_nodes_hat,
                        edges_hard,
                        nodes_hard,
                        nodes_fake2,
                        fake_edge_index2,
                        fake_edge_attr2,
                        g2_attr_for_traconv,
                        g2_nodes_for_traconv,
                    ) = self.G2(
                        edges_logits_forGAN2, nodes_logits_forGAN2, prot_n, prot_e
                    )
                    drugs_node_for_transconv = self.nodes_mlp(
                        drugs.x.view(-1, 1).float()
                    )
                    drugs_attr_for_transconv = self.attr_mlp(
                        drugs.edge_attr.view(-1, 1).float()
                    )

                    if self.dis_select == "conv":

                        logits_fake2, features_fake2 = self.D2(
                            tra_edges_hat, None, tra_nodes_hat
                        )

                    elif self.dis_select == "PNA":

                        logits_fake2 = self.D2_PNA(
                            nodes_fake2, fake_edge_index2, fake_edge_attr2, data.batch
                        )

                    elif self.dis_select == "TraConv":

                        logits_fake2 = self.D2_TraConv(
                            g2_nodes_for_traconv,
                            fake_edge_index2,
                            g2_attr_for_traconv,
                            data.batch,
                        )

                    d2_loss_fake = torch.mean(logits_fake2)

                    if self.dis_select == "conv":

                        logits_real2, features_real2 = self.D2(
                            drugs_a_tensor, None, drugs_x_tensor
                        )

                    elif self.dis_select == "PNA":

                        logits_real2 = self.D2_PNA(
                            drugs.x, drugs.edge_index, drugs.edge_attr, drugs.batch
                        )

                    elif self.dis_select == "TraConv":

                        logits_real2 = self.D2_TraConv(
                            drugs_node_for_transconv,
                            drugs.edge_index,
                            drugs_attr_for_transconv,
                            drugs.batch,
                        )

                    d2_loss_real = -torch.mean(logits_real2)
                    d2_loss = d2_loss_fake + d2_loss_real
                    g2_loss = -torch.mean(logits_fake2)
                    fake_mol2 = [
                        self.dataset.matrices2mol_drugs(
                            n_.data.cpu().numpy(), e_.data.cpu().numpy(), strict=True
                        )
                        for e_, n_ in zip(edges_hard, nodes_hard)
                    ]

                    drugs_mol = [
                        self.dataset.matrices2mol_drugs(
                            n_.data.cpu().numpy(), e_.data.cpu().numpy(), strict=True
                        )
                        for e_, n_ in zip(drugs_a, drugs_x)
                    ]

                # Log update
                m0, m1 = all_scores(
                    fake_mol, mols, norm=True
                )  # 'mols' is output of Fake Reward
                m0 = {k: np.array(v)[np.nonzero(v)].mean() for k, v in m0.items()}
                m0.update(m1)
                m2, m3 = all_scores(fake_mol, mols, norm=True)
                m2 = {k: np.array(v)[np.nonzero(v)].mean() for k, v in m2.items()}
                m2.update(m3)

                sample_path = os.path.join(self.sample_path, "inference", date)
                sample_path_GAN2 = os.path.join(
                    self.sample_path_GAN2, "inference", date
                )
                if not os.path.exists(sample_path_GAN2):
                    os.makedirs(sample_path_GAN2)
                if not os.path.exists(sample_path):
                    os.makedirs(sample_path)
                mols2grid_image(fake_mol, self.sample_path)
                save_smiles_matrices(
                    fake_mol, g_edges_hard.detach(), g_nodes_hard.detach(), sample_path
                )
                mols2grid_image(fake_mol2, self.sample_path_GAN2)
                save_smiles_matrices(
                    fake_mol2,
                    edges_hard.detach(),
                    nodes_hard.detach(),
                    sample_path_GAN2,
                )
                log = "Test started [{}]".format("now")
                for tag, value in m0.items():
                    log += ", {}: {:.4f}".format(tag, value)
                print(log)
                if ((i * self.batch_size) // self.inference_sample_num) >= 1:
                    break


# Compute loss for gradient penalty.

#    eps_node = torch.rand(nodes.size(0)).to(self.device)
#    eps_attr = torch.rand(logits_real.size(0),1,1,).to(self.device)
#    eps_idx = torch.rand(logits_real.size(0),1,1,).to(self.device)
#    x_int0 = (eps * edge_attr + (1. - eps) * fake_edge_attr).requires_grad_(True)
#    x_int1 = (eps.squeeze(-1) * nodes + (1. - eps.squeeze(-1)) * nodes_fake).requires_grad_(True)
#    x_int2 = ((eps.squeeze(-1) * edge_index + (1. - eps.squeeze(-1)) * fake_edge_index).requires_grad_(True))
#
#    grad0= self.D(x_int1, x_int0, x_int2,node_index)
#    d_loss_gp = self.gradient_penalty(grad0, x_int0) + self.gradient_penalty(grad1, x_int1)

##### FEATURE MATCHING

#    if self.feature_matching is True:
#
#    edges_hard, nodes_hard = torch.max(edges_hat, -1)[1], torch.max(nodes_hat, -1)[1]

#    fake_mol = [self.data.matrices2mol(n_.data.cpu().numpy(), e_.data.cpu().numpy(), strict=True)
#                for e_, n_ in zip(edges_hard, nodes_hard)]
#    features_hat = torch.empty(size=(self.batch_size, f.size(1),f.size(2))).to(self.device).long()
#    for iter in range(len(fake_mol)):
#        if fake_mol[iter] != None:
#            features_hat[iter] = torch.from_numpy(self.data._genF(fake_mol[iter]))
#        elif fake_mol[iter] == None:
#            features_hat[iter] = torch.zeros(size=(1, f.size(1),f.size(2)))
#        else:
#            features_hat = None
