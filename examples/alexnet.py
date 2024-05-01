from typing import Callable
import torch
import numpy as np
import torchvision
import torchvision.transforms as transforms


# Core dependencies
import jax
import jax.numpy as jnp
import optax

# pcax
import pcax as px
import pcax.predictive_coding as pxc
import pcax.nn as pxnn
import pcax.utils as pxu
import pcax.functional as pxf

# stune
import stune


class AlexNet(pxc.EnergyModule):
    def __init__(self, nm_classes: int, beta: float, act_fn: Callable[[jax.Array], jax.Array]) -> None:
        super().__init__()

        self.nm_classes = px.static(nm_classes)
        self.beta = px.static(beta)

        # Note we use a custom activation function and not exclusively ReLU since
        # it does not seem to perform as well as in backpropagation
        self.act_fn = px.static(act_fn)

        # We define the convolutional layers. We organise them in blocks just for clarity.
        # Ideally, pcax will soon support a "pxnn.Sequential" module to ease the definition
        # of such blocks. Layers are based on equinox.nn, so check their documentation for
        # more information.
        self.feature_layers = [
            (
                pxnn.Conv2d(3, 64, kernel_size=(3, 3), stride=(2, 2), padding=(1, 1)),
                self.act_fn,
                pxnn.MaxPool2d(kernel_size=2, stride=2),
            ),
            (
                pxnn.Conv2d(64, 192, kernel_size=(3), padding=(1, 1)),
                self.act_fn,
                pxnn.MaxPool2d(kernel_size=2, stride=2),
            ),
            (pxnn.Conv2d(192, 384, kernel_size=(3, 3), padding=(1, 1)), self.act_fn),
            (pxnn.Conv2d(384, 256, kernel_size=(3, 3), padding=(1, 1)), self.act_fn),
            (
                pxnn.Conv2d(256, 256, kernel_size=(3, 3), padding=(1, 1)),
                self.act_fn,
                pxnn.MaxPool2d(kernel_size=2, stride=2),
            ),
        ]
        # We define the classifier layers. We organise them in blocks just for clarity.
        self.classifier_layers = [
            (pxnn.Linear(256 * 2 * 2, 4096), self.act_fn),
            (pxnn.Linear(4096, 4096), self.act_fn),
            (pxnn.Linear(4096, self.nm_classes.get()),),
        ]

        # We define the Vode modules. Note that currently each vode requires its shape
        # to be manually specified. This will be improved in the near future as lazy
        # initialisation should be possible.
        self.vodes = (
            [
                pxc.Vode(shape)
                for _, shape in zip(
                    range(len(self.feature_layers)), [(64, 8, 8), (192, 4, 4), (384, 4, 4), (256, 4, 4), (256, 2, 2)]
                )
            ]
            + [pxc.Vode((4096,)) for _ in range(len(self.classifier_layers) - 1)]
            + [pxc.Vode((self.nm_classes.get(),))]
        )

        # Remember 'frozen' is a user specified attribute used later in the gradient function
        self.vodes[-1].h.frozen = True

    def __call__(self, x: jax.Array, y: jax.Array):
        # Nothing new here: we just define the forward pass of the network by iterating
        # through the blocks and vodes. Each block is followed by a vode, to split the
        # computation in indpendent chunks.
        for block, node in zip(self.feature_layers, self.vodes[: len(self.feature_layers)]):
            for layer in block:
                x = layer(x)
            x = node(x)

        x = x.flatten()
        for block, node in zip(self.classifier_layers, self.vodes[len(self.feature_layers) :]):
            for layer in block:
                x = layer(x)
            x = node(x)

        if y is not None:
            self.vodes[-1].set("h", self.vodes[-1].get("u") - self.beta.get() * (self.vodes[-1].get("u") - y))

        return self.vodes[-1].get("u")


# This is a simple collate function that stacks numpy arrays used to interface
# the PyTorch dataloader with JAX. In the future we hope to provide custom dataloaders
# that are independent of PyTorch.


def numpy_collate(batch):
    if isinstance(batch[0], np.ndarray):
        return np.stack(batch)
    elif isinstance(batch[0], (tuple, list)):
        transposed = zip(*batch)
        return [numpy_collate(samples) for samples in transposed]
    else:
        return np.array(batch)


# The dataloader assumes cuda is being used, as such it sets 'pin_memory = True' and
# 'prefetch_factor = 2'. Note that the batch size should be constant during training, so
# we set 'drop_last = True' to avoid having to deal with variable batch sizes.
class TorchDataloader(torch.utils.data.DataLoader):
    def __init__(
        self,
        dataset,
        batch_size=1,
        shuffle=None,
        sampler=None,
        batch_sampler=None,
        num_workers=1,
        pin_memory=True,
        timeout=0,
        worker_init_fn=None,
        persistent_workers=True,
        prefetch_factor=2,
    ):
        super(self.__class__, self).__init__(
            dataset,
            batch_size=batch_size,
            shuffle=shuffle,
            sampler=sampler,
            batch_sampler=batch_sampler,
            num_workers=num_workers,
            collate_fn=numpy_collate,
            pin_memory=pin_memory,
            drop_last=True if batch_sampler is None else None,
            timeout=timeout,
            worker_init_fn=worker_init_fn,
            persistent_workers=persistent_workers,
            prefetch_factor=prefetch_factor,
        )


def get_dataloaders(batch_size: int):
    t = transforms.Compose(
        [
            transforms.ToTensor(),
            # These are normalisation factors found online.
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
            lambda x: x.numpy(),
        ]
    )

    train_dataset = torchvision.datasets.CIFAR10(
        "~/tmp/cifar10/",
        transform=t,
        download=True,
        train=True,
    )

    train_dataloader = TorchDataloader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=4,
    )

    test_dataset = torchvision.datasets.CIFAR10(
        "~/tmp/cifar10/",
        transform=t,
        download=True,
        train=False,
    )

    test_dataloader = TorchDataloader(
        test_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=4,
    )

    return train_dataloader, test_dataloader


@pxf.vmap(pxu.Mask(pxc.VodeParam | pxc.VodeParam.Cache, (None, 0)), in_axes=(0, 0), out_axes=0)
def forward(x, y, *, model: AlexNet):
    return model(x, y)


@pxf.vmap(pxu.Mask(pxc.VodeParam | pxc.VodeParam.Cache, (None, 0)), in_axes=(0,), out_axes=(None, 0), axis_name="batch")
def energy(x, *, model: AlexNet):
    y_ = model(x, None)
    return jax.lax.pmean(model.energy().sum(), "batch"), y_


@pxf.jit(static_argnums=0)
def train_on_batch(T: int, x: jax.Array, y: jax.Array, *, model: AlexNet, optim_w: pxu.Optim, optim_h: pxu.Optim):
    model.train()
    optim_h.init(model)

    # Init step
    with pxu.step(model, pxc.STATUS.INIT, clear_params=pxc.VodeParam.Cache):
        forward(x, y, model=model)

    r = []
    # Inference steps
    for _ in range(T):
        with pxu.step(model, clear_params=pxc.VodeParam.Cache):
            _, g = pxf.value_and_grad(pxu.Mask(pxu.m(pxc.VodeParam).has_not(frozen=True), [False, True]), has_aux=True)(
                energy
            )(x, model=model)

        updates = optim_h.step(model, g["model"])
        r.append(
            jnp.stack(
                [
                    jnp.mean(jnp.linalg.norm(jnp.reshape(u.h.get(), (-1, x.shape[0])), axis=-1))
                    for u in updates.vodes[:-1]
                ]
            )
        )

    # Learning step
    with pxu.step(model, clear_params=pxc.VodeParam.Cache):
        _, g = pxf.value_and_grad(pxu.Mask(pxnn.LayerParam, [False, True]), has_aux=True)(energy)(x, model=model)
    optim_w.step(model, g["model"])

    return jnp.stack(r)


@pxf.jit()
def eval_on_batch(x: jax.Array, y: jax.Array, *, model: AlexNet):
    model.eval()

    with pxu.step(model, pxc.STATUS.INIT, clear_params=pxc.VodeParam.Cache):
        y_ = forward(x, None, model=model).argmax(axis=-1)

    return (y_ == y).mean(), y_


def train(dl, T, *, model: AlexNet, optim_w: pxu.Optim, optim_h: pxu.Optim):
    for i, (x, y) in enumerate(dl):
        train_on_batch(
            T, x, jax.nn.one_hot(y, model.nm_classes.get()), model=model, optim_w=optim_w, optim_h=optim_h
        )


def eval(dl, *, model: AlexNet):
    acc = []
    ys_ = []

    for x, y in dl:
        a, y_ = eval_on_batch(x, y, model=model)
        acc.append(a)
        ys_.append(y_)

    return np.mean(acc), np.concatenate(ys_)


def main(run_info: stune.RunInfo):
    batch_size = run_info["hp/batch_size"]
    nm_epochs = run_info["hp/epochs"]

    model = AlexNet(nm_classes=10, beta=run_info["hp/beta"], act_fn=getattr(jax.nn, run_info["hp/act_fn"]))

    with pxu.step(model, pxc.STATUS.INIT, clear_params=pxc.VodeParam.Cache):
        forward(jnp.zeros((batch_size, 3, 32, 32)), None, model=model)

        optim_h = pxu.Optim(
            optax.chain(
                optax.sgd(run_info["hp/optim/x/lr"] * batch_size, momentum=run_info["hp/optim/x/momentum"]),
            ),
            pxu.Mask(pxc.VodeParam)(model),
        )
        optim_w = pxu.Optim(optax.adamw(run_info["hp/optim/w/lr"] / model.beta.get()), pxu.Mask(pxnn.LayerParam)(model))

    train_dataloader, test_dataloader = get_dataloaders(batch_size)

    best_accuracy = 0
    for e in range(nm_epochs):
        train(train_dataloader, T=run_info["hp/T"], model=model, optim_w=optim_w, optim_h=optim_h)
        a, y = eval(test_dataloader, model=model)
        if run_info.study_name is None:
            print(f"Epoch {e + 1}/{nm_epochs} - Test Accuracy: {a * 100:.2f}%")
        best_accuracy = max(best_accuracy, a)

    return best_accuracy


if __name__ == "__main__":
    main(None)
