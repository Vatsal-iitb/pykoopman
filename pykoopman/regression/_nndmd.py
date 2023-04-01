"""module for implementing a neural network DMD"""
from __future__ import annotations

import pickle
from abc import abstractmethod
from warnings import warn

import lightning as L
import numpy as np
import torch
from sklearn.utils.validation import check_is_fitted
from torch import nn
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import DataLoader
from torch.utils.data import Dataset

from pykoopman.regression._base import BaseRegressor


# todo: add the control version


class MaskedMSELoss(nn.Module):
    """
    Calculates the mean squared error (MSE) loss between `output` and `target`, with
    masking based on `target_lens`.

    Args:
        max_look_forward

    Returns:
        The MSE loss as a scalar tensor.
    """

    def __init__(self, max_look_forward):
        super().__init__()
        self.max_look_forward = torch.tensor(max_look_forward, dtype=torch.int)
        # self.register_buffer("mask", torch.zeros_like(target, dtype=torch.bool))

    def forward(self, output, target, target_lens):
        """
        Calculates the MSE loss between `output` and `target`, with masking based on
        `target_lens`.

        Args:
            output (torch.Tensor): The output tensor of shape (batch_size,
            sequence_length, features).
            target (torch.Tensor): The target tensor of shape (batch_size,
            sequence_length, features).
            target_lens (torch.Tensor): A tensor of shape (batch_size,) containing the
            sequence lengths for each item in the batch.

        Returns:
            The MSE loss as a scalar tensor.
        """
        # Create mask using target_lens
        mask = torch.zeros_like(output, dtype=torch.bool)
        for i, length in enumerate(target_lens):
            if length > self.max_look_forward:
                length_used = self.max_look_forward
            else:
                length_used = length
            mask[i, :length_used, :] = 1

        # Compute squared differences and apply mask
        squared_diff = torch.pow(output - target, 2)
        squared_diff_masked = torch.where(
            mask, squared_diff, torch.zeros_like(squared_diff)
        )

        # Compute the MSE loss
        mse_loss = squared_diff_masked.sum() / mask.sum()

        return mse_loss


class FFNN(nn.Module):
    """A feedforward neural network with customizable architecture and activation
    functions.

    Args:
        input_size (int): The size of the input layer.
        hidden_sizes (list): A list of the sizes of the hidden layers.
        output_size (int): The size of the output layer.
        activations (str): A string for activation functions for every layer.

    Attributes:
        layers (nn.ModuleList): A list of the neural network layers.
    """

    def __init__(self, input_size, hidden_sizes, output_size, activations):
        super(FFNN, self).__init__()

        activations_dict = {
            "relu": nn.ReLU(),
            "sigmoid": nn.Sigmoid(),
            "tanh": nn.Tanh(),
            "swish": nn.SiLU(),
            "elu": nn.ELU(),
            "mish": nn.Mish(),
            "linear": nn.Identity(),
        }

        # Define the activation
        act = activations_dict[activations]

        # Define the input layer
        self.layers = nn.ModuleList()

        # if linear layer, remove bias
        if activations == "linear":
            bias = False
        else:
            bias = True

        if len(hidden_sizes) == 0:
            self.layers.append(nn.Linear(input_size, output_size, bias))
        else:
            self.layers.append(nn.Linear(input_size, hidden_sizes[0], bias))
            if activations != "linear":
                self.layers.append(act)

            # Define the hidden layers
            for i in range(1, len(hidden_sizes)):
                self.layers.append(
                    nn.Linear(hidden_sizes[i - 1], hidden_sizes[i], bias)
                )
                if activations != "linear":
                    self.layers.append(act)

            # Define the last output layer
            bias_last = False  # True  # last layer with bias
            self.layers.append(nn.Linear(hidden_sizes[-1], output_size, bias_last))

    def forward(self, x):
        """Performs a forward pass through the neural network.

        Args:
            x (torch.Tensor): The input tensor to the neural network.

        Returns:
            torch.Tensor: The output tensor of the neural network.
        """
        for layer in self.layers:
            x = layer(x)
        return x


class BaseKoopmanOperator(nn.Module):
    def __init__(
        self,
        dim: int,
        dt: float = 1.0,
        init_std: float = 0.1,
    ):
        super().__init__()
        self.dim = dim
        self.register_buffer("dt", torch.tensor(dt))
        self.init_std = init_std

    def forward(self, x):
        koopman_operator = self.get_discrete_time_Koopman_Operator()
        xnext = torch.matmul(x, koopman_operator.t())  # following pytorch convention
        return xnext

    def get_discrete_time_Koopman_Operator(self):
        return torch.matrix_exp(self.dt * self.get_K())

    @abstractmethod
    def get_K(self):
        pass


class StandardKoopmanOperator(BaseKoopmanOperator):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.register_parameter(
            "K",
            torch.nn.Parameter(
                torch.nn.init.trunc_normal_(
                    torch.zeros(self.dim, self.dim), std=self.init_std
                )
            ),
        )

    def get_K(self):
        return self.K


class HamiltonianKoopmanOperator(BaseKoopmanOperator):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.register_parameter(
            "off_diagonal",
            torch.nn.Parameter(
                torch.nn.init.trunc_normal_(
                    torch.zeros(self.dim, self.dim), std=self.init_std
                )
            ),
        )

    def get_K(self):
        return self.off_diagonal - self.off_diagonal.t()


class DissipativeKoopmanOperator(BaseKoopmanOperator):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.register_parameter(
            "off_diagonal",
            torch.nn.Parameter(
                torch.nn.init.trunc_normal_(
                    torch.zeros(self.dim, self.dim), std=self.init_std
                )
            ),
        )
        self.register_parameter(
            "diagonal",
            torch.nn.Parameter(
                -torch.pow(
                    torch.nn.init.trunc_normal_(
                        torch.zeros(self.dim), std=self.init_std
                    ),
                    2,
                )
            ),
        )

    def get_K(self):
        return torch.diag(self.diagonal) + self.off_diagonal - self.off_diagonal.t()


class DLKoopmanRegressor(L.LightningModule):
    """
    Deep Learning Koopman Regressor module, implemented using PyTorch Lightning.

    Args:
        config_encoder (dict): Configuration dictionary for the encoder neural network.
        config_decoder (dict): Configuration dictionary for the decoder neural network.
        config_koopman (dict): Configuration dictionary for the Stable Koopman Operator.
        config_train (dict): Configuration dictionary for the training process.

    Attributes:
        input_size (int): The size of the input tensor.
        output_size (int): The size of the output tensor.
        _encoder (FFNN): The encoder neural network.
        _decoder (FFNN): The decoder neural network.
        _koopman_propagator (StableKoopmanOperator): The Stable Koopman Operator.
        _dt (torch.Tensor): The timestep size for the Koopman operator.
        look_forward (int): The number of timesteps to look forward during training.
        using_lbfgs (bool): Whether to use the LBFGS optimizer during training.
        masked_loss_metric (MaskedMSELoss): The masked mean squared error loss metric.
    """

    def __init__(
        self,
        mode=None,
        dt=1.0,
        look_forward=1,
        config_encoder={},
        config_decoder={},
        lbfgs=False,
    ):
        super(DLKoopmanRegressor, self).__init__()

        self.input_size = config_encoder["input_size"]
        self.output_size = config_encoder["output_size"]

        self._encoder = FFNN(
            input_size=config_encoder["input_size"],
            hidden_sizes=config_encoder["hidden_sizes"],
            output_size=config_encoder["output_size"],
            activations=config_encoder["activations"],
        )

        self._decoder = FFNN(
            input_size=config_decoder["input_size"],
            hidden_sizes=config_decoder["hidden_sizes"],
            output_size=config_decoder["output_size"],
            activations=config_decoder["activations"],
        )

        if mode == "Dissipative":
            self._koopman_propagator = DissipativeKoopmanOperator(
                dim=config_encoder["output_size"], dt=dt, init_std=1e-1
            )
        elif mode == "Hamiltonian":
            self._koopman_propagator = HamiltonianKoopmanOperator(
                dim=config_encoder["output_size"], dt=dt, init_std=1e-1
            )
        else:
            self._koopman_propagator = StandardKoopmanOperator(
                dim=config_encoder["output_size"], dt=dt, init_std=1e-1
            )

        self.look_forward = look_forward
        self.using_lbfgs = lbfgs

        self.masked_loss_metric = MaskedMSELoss(1)
        # self.masked_loss_metric = MaskedMSELoss(self.look_forward)

        if self.using_lbfgs:
            self.automatic_optimization = False

            def training_step(batch, batch_idx):
                optimizer = self.optimizers()

                def closure():

                    # unpack batch data
                    x, y, ys = batch

                    # get the max look forward in this batch
                    batch_look_forward = ys.max()

                    # encode x
                    encoded_x = self._encoder(x)

                    # future unroll look_forward
                    phi_seq = self._propagate_encoded_n_steps(
                        encoded_x, n=batch_look_forward
                    )

                    # standard RNN loss
                    decoded_y_seq_rnn = torch.zeros(
                        (x.size(0), self.look_forward, self.input_size),
                        device=self.device,
                    )

                    for i in range(batch_look_forward):
                        decoded_y_seq_rnn[:, i, :] = self._decoder(phi_seq[:, i, :])
                    rnn_loss = self.masked_loss_metric(decoded_y_seq_rnn, y, ys)

                    # autoencoder reconstruction loss
                    # for x
                    decoded_x = self._decoder(encoded_x)
                    rec_loss = torch.nn.functional.mse_loss(decoded_x, x)

                    # for y_seq
                    decoded_y_seq_rec = torch.zeros(
                        (x.size(0), self.look_forward, self.input_size),
                        device=self.device,
                    )
                    for i in range(batch_look_forward):
                        decoded_y_seq_rec[:, i, :] = self._decoder(
                            self._encoder(y[:, i, :])
                        )
                    rec_loss += self.masked_loss_metric(decoded_y_seq_rec, y, ys)

                    loss = rnn_loss + rec_loss

                    optimizer.zero_grad()
                    self.manual_backward(loss)

                    self.log("loss", loss, prog_bar=True)
                    self.log("rec_loss", rec_loss, prog_bar=True)
                    self.log("rnn_loss", rnn_loss, prog_bar=True)

                    return loss

                optimizer.step(closure=closure)

            self.training_step = training_step

        self.save_hyperparameters()

    def forward(self, x, n=1):
        """
        Forward pass of the DLKoopmanRegressor model.

        Args:
            x (torch.Tensor): Input tensor of shape (batch_size, input_size).
            n (int): Number of steps to propagate in the encoded space (default: 1).

        Returns:
            torch.Tensor: Decoded tensor of shape (batch_size, output_size).
        """
        encoded = self._encoder(x)
        phi_seq = self._propagate_encoded_n_steps(encoded, n)
        decoded = self._decoder(phi_seq[:, -1, :])
        return decoded

    def forward_all(self, x, n):
        encoded = self._encoder(x)
        phi_seq = self._propagate_encoded_n_steps(encoded, n)
        decoded = torch.zeros(x.size(0), n, self.input_size)
        for i in range(n):
            decoded[:, i, :] = self._decoder(phi_seq[:, i, :])
        return decoded

    def _propagate_encoded_n_steps(self, encoded, n):
        """
        Propagates the encoded tensor linearly in the encoded space for n steps.

        Args:
            encoded (torch.Tensor): The encoded tensor of shape (batch_size,
            encoded_size). n (int): The number of steps to propagate.

        Returns:
            torch.Tensor: The propagated encoded tensor of shape (batch_size, n,
            encoded_size).
        """
        encoded_future = []
        for i in range(n):
            encoded = self._koopman_propagator(encoded)
            encoded_future.append(encoded)
        return torch.stack(encoded_future, 1)

    def training_step(self, batch, batch_idx):
        """
        Defines a training step for the DL Koopman Regressor.

        Args:
            batch: tuple of (x, y, ys), representing the input data,
                the true output data, and the sequence length for
                each sample in the batch.
            batch_idx: integer, the index of the batch.

        Returns:
            tensor representing the loss value for this training step.
        """
        # unpack batch data
        x, y, ys = batch

        # get the max look forward in this batch
        batch_look_forward = ys.max()

        # encode x
        encoded_x = self._encoder(x)

        # future unroll look_forward
        phi_seq = self._propagate_encoded_n_steps(encoded_x, n=batch_look_forward)

        # standard RNN loss
        decoded_y_seq_rnn = torch.zeros(
            (x.size(0), self.look_forward, self.input_size), device=self.device
        )

        for i in range(batch_look_forward):
            decoded_y_seq_rnn[:, i, :] = self._decoder(phi_seq[:, i, :])
        rnn_loss = self.masked_loss_metric(decoded_y_seq_rnn, y, ys)

        # autoencoder reconstruction loss
        # for x
        decoded_x = self._decoder(encoded_x)
        rec_loss = torch.nn.functional.mse_loss(decoded_x, x)

        # for y_seq
        decoded_y_seq_rec = torch.zeros(
            (x.size(0), self.look_forward, self.input_size), device=self.device
        )
        for i in range(batch_look_forward):
            decoded_y_seq_rec[:, i, :] = self._decoder(self._encoder(y[:, i, :]))
        rec_loss += self.masked_loss_metric(decoded_y_seq_rec, y, ys)

        loss = rnn_loss + rec_loss

        self.log("loss", loss, prog_bar=True)
        self.log("rec_loss", rec_loss, prog_bar=True)
        self.log("rnn_loss", rnn_loss, prog_bar=True)
        return loss

    def configure_optimizers(self):
        """Configures and returns the optimizer to use for training.

        If using LBFGS optimizer, set `using_lbfgs` attribute to True when
        initializing the DLKoopmanRegressor instance.

        Returns:
            An instance of torch.optim.Optimizer to use for training.
        """
        if self.using_lbfgs:
            optimizer = torch.optim.LBFGS(
                self.parameters(),
                lr=1,
                history_size=100,
                max_iter=20,
                line_search_fn="strong_wolfe",
            )
        else:
            optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        return optimizer


class SeqDataDataset(Dataset):
    def __init__(self, x, y, ys, transform=None):
        self.x = x.squeeze(1)
        self.y = y
        self.ys = ys
        self.normalization = transform

    def __len__(self):
        return len(self.ys)

    def __getitem__(self, idx):
        x = self.x[idx].clone()
        y = self.y[idx].clone()
        ys = self.ys[idx].clone()

        if self.normalization:
            x = self.normalization(x)
            y = self.normalization(y)

        return x, y, ys


class TensorNormalize(nn.Module):
    def __init__(self, mean, std):
        super().__init__()
        self.mean = mean
        self.std = std

    def forward(self, tensor: torch.Tensor):
        return torch.divide((tensor - self.mean), self.std)
        # return # tensor.copy_(tensor.sub_(self.mean).div_(self.std))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(mean={self.mean}, std={self.std})"


class InverseTensorNormalize(nn.Module):
    def __init__(self, mean, std):
        super().__init__()
        self.mean = mean
        self.std = std

    def forward(self, tensor: torch.Tensor):
        return torch.multiply(tensor, self.std) + self.mean
        # return tensor.copy_(tensor.mul_(self.std).add_(self.mean))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(mean={self.mean}, std={self.std})"


class SeqDataModule(L.LightningDataModule):
    def __init__(
        self,
        data_tr,
        data_val,
        look_forward=10,
        batch_size=32,
        normalize=True,
        normalize_mode="equal",
        normalize_std_factor=2.0,
    ):
        super().__init__()
        # input data_tr or data_val is a list of 2D np.ndarray. each 2d
        # np.ndarray is a trajectory, and the axis 0 is number of samples, axis 1 is
        # the number of system state
        self.data_tr = data_tr
        self.data_val = data_val
        self.look_forward = look_forward
        self.batch_size = batch_size
        self.look_back = 1
        self.normalize = normalize
        self.normalize_mode = normalize_mode
        self.normalization = None
        self.inverse_transform = None
        self.normalize_std_factor = normalize_std_factor

    def prepare_data(self):
        # train data
        if self.data_tr is None:
            raise ValueError("You must feed training data!")
        if isinstance(self.data_tr, list):
            data_list = self.data_tr
        elif isinstance(self.data_tr, str):
            f = open(self.data_tr, "rb")
            data_list = pickle.load(f)
        else:
            raise ValueError("Wrong type of `self.data_tr`")

        # check train data
        data_list = self.check_list_of_nparray(data_list)

        # find the mean, std
        if self.normalize:
            stacked_data_list = np.vstack(data_list)
            mean = stacked_data_list.mean(axis=0)
            std = stacked_data_list.std(axis=0)

            # zero mean so easier for downstream
            self.mean = torch.FloatTensor(mean) * 0
            # default = 2.0, more stable
            self.std = torch.FloatTensor(std) * self.normalize_std_factor

            if self.normalize_mode == "max":
                self.std = torch.ones_like(self.std) * self.std.max()

            # prevent divide by zero error
            for i in range(len(self.std)):
                if self.std[i] < 1e-6:
                    self.std[i] += 1e-3

            # get transform
            self.normalization = TensorNormalize(self.mean, self.std)

            # get inverse transform
            self.inverse_transform = InverseTensorNormalize(self.mean, self.std)

        # create time-delayed data
        self._tr_x, self._tr_yseq, self._tr_ys = self.convert_seq_list_to_delayed_data(
            data_list, self.look_back, self.look_forward
        )

        # validation data
        if self.data_val is not None:
            # raise ValueError("You need to feed validation data!")
            if isinstance(self.data_val, list):
                data_list = self.data_val
            elif isinstance(self.data_val, str):
                f = open(self.data_val, "rb")
                data_list = pickle.load(f)
            else:
                raise ValueError("Wrong type of `self.data_val`")

            # check val data
            data_list = self.check_list_of_nparray(data_list)

            # create time-delayed data
            (
                self._val_x,
                self._val_yseq,
                self._val_ys,
            ) = self.convert_seq_list_to_delayed_data(
                data_list, self.look_back, self.look_forward
            )
        else:
            warn("Warning: no validation data prepared")

    def setup(self, stage=None):
        # Load data and split into train and validation sets here
        # Assign train/val datasets for use in dataloaders
        if stage == "fit":
            self.tr_dataset = SeqDataDataset(
                self._tr_x, self._tr_yseq, self._tr_ys, self.normalization
            )
            if self.data_val is not None:
                self.val_dataset = SeqDataDataset(
                    self._val_x, self._val_yseq, self._val_ys, self.normalization
                )
        else:
            raise NotImplementedError("We didn't implement for stage not `fit`")

    def train_dataloader(self):
        return DataLoader(
            self.tr_dataset, self.batch_size, shuffle=True, collate_fn=self.collate_fn
        )

    def val_dataloader(self):
        return DataLoader(
            self.val_dataset, self.batch_size, shuffle=True, collate_fn=self.collate_fn
        )

    def convert_seq_list_to_delayed_data(self, data_list, look_back, look_forward):
        time_delayed_x_list = []
        time_delayed_yseq_list = []
        for seq in data_list:
            # if self.look_forward + self.look_back > len(seq):
            #     raise ValueError("look_forward too large")
            n_sub_traj = len(seq) - look_back - look_forward + 1
            if n_sub_traj >= 1:
                for i in range(len(seq) - look_back - look_forward + 1):
                    time_delayed_x_list.append(seq[i : i + look_back])
                    time_delayed_yseq_list.append(
                        seq[i + look_back : i + look_back + look_forward]
                    )
            else:
                # only 1 traj, just to predict to its end
                time_delayed_x_list.append(seq[0:1])
                time_delayed_yseq_list.append(seq[1:])
        time_delayed_yseq_lens_list = [x.shape[0] for x in time_delayed_yseq_list]

        # convert data to tensor
        time_delayed_x = torch.FloatTensor(np.array(time_delayed_x_list))
        time_delayed_yseq = pad_sequence(
            [torch.FloatTensor(x) for x in time_delayed_yseq_list], True
        )
        time_delayed_yseq_lens = torch.LongTensor(time_delayed_yseq_lens_list)
        return time_delayed_x, time_delayed_yseq, time_delayed_yseq_lens

    def collate_fn(self, batch):
        x_batch, y_batch, ys_batch = zip(*batch)
        xx = torch.stack(x_batch, 0)
        yy = torch.stack(y_batch, 0)
        ys = torch.stack(ys_batch, 0)
        return xx, yy, ys

    @classmethod
    def check_list_of_nparray(cls, data_list):
        # check if data is complex
        if any(np.iscomplexobj(x) for x in data_list):
            raise TypeError("Complex data is not supported")

        # check if data has float64
        if any(x.dtype is np.float64 for x in data_list):
            warn("Found float64 data. Will convert to float32")

        # convert data to float32 if float64
        for i, data_traj in enumerate(data_list):
            if "float" not in data_traj.dtype.name:
                raise TypeError("Found data is not float")
            if data_traj.dtype.name == "float64":
                data_list[i] = data_traj.astype("float32")

        return data_list


class NNDMD(BaseRegressor):
    def __init__(
        self,
        mode=None,
        dt=1.0,
        look_forward=1,
        config_encoder=dict(
            input_size=2, hidden_sizes=[32] * 2, output_size=6, activations="tanh"
        ),
        config_decoder=dict(
            input_size=6, hidden_sizes=[32] * 2, output_size=2, activations="linear"
        ),
        batch_size=16,
        lbfgs=False,
        normalize=True,
        normalize_mode="equal",
        normalize_std_factor=2.0,
        trainer_kwargs={},
    ):
        self.mode = mode
        self.look_forward = look_forward
        self.config_encoder = config_encoder
        self.config_decoder = config_decoder
        self.lbfgs = lbfgs
        self.normalize = normalize
        self.normalize_mode = normalize_mode
        self.dt = dt
        self.trainer_kwargs = trainer_kwargs
        self.normalize_std_factor = normalize_std_factor
        self.batch_size = batch_size

        # build DLK regressor
        self._regressor = DLKoopmanRegressor(
            mode, dt, look_forward, config_encoder, config_decoder, lbfgs
        )

    def fit(self, x, y=None, dt=None):
        """
        ..note:
            `n_samples_` is meaningless here
            this dt argument is just to please regressor class, no real use.
        """
        # build trainer
        self.trainer = L.Trainer(**self.trainer_kwargs)

        self.n_input_features_ = self.config_encoder["input_size"]

        # create the data module
        # case: a single traj, x is 2D np.ndarray, no validation
        if y is None and isinstance(x, np.ndarray) and x.ndim == 2:
            t0, t1 = x[:-1], x[1:]
            list_of_traj = [np.stack((t0[i], t1[i]), 0) for i in range(len(x) - 1)]
            self.dm = SeqDataModule(
                list_of_traj,
                None,
                self.look_forward,
                self.batch_size,
                self.normalize,
                self.normalize_mode,
                self.normalize_std_factor,
            )
            self.n_samples_ = len(list_of_traj)

            # case: x, y are 2D np.ndarray, no validation
        elif (
            isinstance(x, np.ndarray)
            and isinstance(y, np.ndarray)
            and x.ndim == 2
            and y.ndim == 2
        ):
            t0, t1 = x, y
            list_of_traj = [np.stack((t0[i], t1[i]), 0) for i in range(len(x) - 1)]
            self.dm = SeqDataModule(
                list_of_traj,
                None,
                self.look_forward,
                self.batch_size,
                self.normalize,
                self.normalize_mode,
                self.normalize_std_factor,
            )
            self.n_samples_ = len(list_of_traj)

        # case: only training data, x is a list of trajectories, y is None
        elif isinstance(x, list) and y is None:
            self.dm = SeqDataModule(
                x,
                None,
                self.look_forward,
                self.batch_size,
                self.normalize,
                self.normalize_mode,
                self.normalize_std_factor,
            )
            self.n_samples_ = len(x)

        # case: x, y are two lists of trajectories, we have validation data
        elif isinstance(x, list) and isinstance(y, list):
            self.dm = SeqDataModule(
                x,
                y,
                self.look_forward,
                self.batch_size,
                self.normalize,
                self.normalize_mode,
                self.normalize_std_factor,
            )
            self.n_samples_ = len(x)
        else:
            raise ValueError("check `x` and `y` for `self.fit`")

        # trainer starts to train
        self.trainer.fit(self._regressor, self.dm)

        # compute Koopman operator information
        self._state_matrix_ = (
            self._regressor._koopman_propagator.get_discrete_time_Koopman_Operator()
            .detach()
            .numpy()
        )
        [self._eigenvalues_, self._eigenvectors_] = np.linalg.eig(self._state_matrix_)

        self._coef_ = self._state_matrix_

        # obtain effective linear transformation
        decoder_weight_list = []
        for i in range(len(self._regressor._decoder.layers)):
            decoder_weight_list.append(
                self._regressor._decoder.layers[i].weight.detach().numpy()
            )
        if len(decoder_weight_list) > 1:
            self._ur = np.linalg.multi_dot(decoder_weight_list[::-1])
        else:
            self._ur = decoder_weight_list[0]

        if self.normalize:
            std = self.dm.inverse_transform.std
            self._ur = np.diag(std) @ self._ur

        # todo: remove _unnormalized_modes, they seem to be useless
        self._unnormalized_modes = self._ur @ self._eigenvectors_

    def predict(self, x, n=1):
        """make prediction of system state after n steps away from x_0=x

        - By default, model is stored on CPU for inference.
        - The result will be returned as numpy.ndarray
        """
        self._regressor.eval()
        x = self._convert_input_ndarray_to_tensor(x)

        with torch.no_grad():
            # print("inference device = ", self._regressor.device)

            if self.normalize:
                y = self.dm.normalization(x)
                y = self._regressor(y, n)
                y = self.dm.inverse_transform(y).numpy()
            else:
                y = self._regressor(x, n).numpy()
            return y

    def _compute_phi(self, x):
        """Returns `phi(x)` given `x`"""
        self._regressor.eval()
        x = self._convert_input_ndarray_to_tensor(x)

        if self.normalize:
            x = self.dm.normalization(x)
        phi = self._regressor._encoder(x).detach().numpy().T
        return phi

    def _compute_psi(self, x):
        phi = self._compute_phi(x)
        psi = np.linalg.inv(self._eigenvectors_) @ phi
        return psi

    def _convert_input_ndarray_to_tensor(self, x):
        if isinstance(x, np.ndarray):
            if x.ndim > 2:
                raise ValueError("input array should be 1 or 2D")
            if x.ndim == 1:
                x = x.reshape(1, -1)
            # convert to a float32
            # if x.dtype == np.float64:
            x = torch.FloatTensor(x)
        elif isinstance(x, torch.Tensor):
            if x.ndim != 2:
                raise ValueError("input tensor `x` must be a 2d tensor")
        return x

    @property
    def coef_(self):
        check_is_fitted(self, "_coef_")
        return self._coef_

    @property
    def state_matrix_(self):
        return self._state_matrix_

    @property
    def eigenvalues_(self):
        check_is_fitted(self, "_eigenvalues_")
        return self._eigenvalues_

    @property
    def eigenvectors_(self):
        check_is_fitted(self, "_eigenvectors_")
        return self._eigenvectors_

    @property
    def unnormalized_modes(self):
        check_is_fitted(self, "_unnormalized_modes")
        return self._unnormalized_modes

    @property
    def ur(self):
        check_is_fitted(self, "_ur")
        return self._ur


if __name__ == "__main__":
    pass
