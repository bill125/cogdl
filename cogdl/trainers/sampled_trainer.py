from abc import ABC, abstractmethod
import copy
import time

import numpy as np
import torch
import torch.nn.functional as F
from tqdm import tqdm

from cogdl.data import Dataset, Data
from cogdl.data.sampler import NodeSampler, EdgeSampler, RWSampler, MRWSampler, LayerSampler, NeighborSampler
from cogdl.models.supervised_model import (
    SupervisedHeterogeneousNodeClassificationModel,
    SupervisedHomogeneousNodeClassificationModel,
)
from cogdl.trainers.supervised_trainer import SupervisedHeterogeneousNodeClassificationTrainer


class SampledTrainer(SupervisedHeterogeneousNodeClassificationTrainer):
    @abstractmethod
    def fit(self, model: SupervisedHeterogeneousNodeClassificationModel, dataset: Dataset):
        raise NotImplementedError

    @abstractmethod
    def _train_step(self):
        pass

    @abstractmethod
    def _test_step(self, split="val"):
        pass

    def __init__(self, args):
        self.device = args.device_id[0] if not args.cpu else "cpu"
        self.patience = args.patience
        self.max_epoch = args.max_epoch
        self.lr = args.lr
        self.weight_decay = args.weight_decay

    @classmethod
    def build_trainer_from_args(cls, args):
        return cls(args)

    def train(self):
        epoch_iter = tqdm(range(self.max_epoch))
        patience = 0
        best_score = 0
        # best_loss = np.inf
        max_score = 0
        min_loss = np.inf
        best_model = copy.deepcopy(self.model)
        for epoch in epoch_iter:
            self._train_step()
            train_acc, _ = self._test_step(split="train")
            val_acc, val_loss = self._test_step(split="val")
            epoch_iter.set_description(f"Epoch: {epoch:03d}, Train: {train_acc:.4f}, Val: {val_acc:.4f}")
            if val_loss <= min_loss or val_acc >= max_score:
                if val_acc >= best_score:  # SAINT loss is not accurate
                    # best_loss = val_loss
                    best_score = val_acc
                    best_model = copy.deepcopy(self.model)
                min_loss = np.min((min_loss, val_loss))
                max_score = np.max((max_score, val_acc))
                patience = 0
            else:
                patience += 1
                if patience == self.patience:
                    self.model = best_model
                    epoch_iter.close()
                    break
        return best_model


class SAINTTrainer(SampledTrainer):
    def __init__(self, args):
        super(SAINTTrainer, self).__init__(args)
        self.args_sampler = self.sampler_from_args(args)

    @classmethod
    def build_trainer_from_args(cls, args):
        return cls(args)

    def sampler_from_args(self, args):
        args_sampler = {
            "sampler": args.sampler,
            "sample_coverage": args.sample_coverage,
            "size_subgraph": args.size_subgraph,
            "num_walks": args.num_walks,
            "walk_length": args.walk_length,
            "size_frontier": args.size_frontier,
        }
        return args_sampler

    def fit(self, model: SupervisedHeterogeneousNodeClassificationModel, dataset: Dataset):
        self.data = dataset.data
        self.data.apply(lambda x: x.to(self.device))
        self.model = model
        if self.args_sampler["sampler"] == "node":
            self.sampler = NodeSampler(self.data, self.args_sampler)
        elif self.args_sampler["sampler"] == "edge":
            self.sampler = EdgeSampler(self.data, self.args_sampler)
        elif self.args_sampler["sampler"] == "rw":
            self.sampler = RWSampler(self.data, self.args_sampler)
        elif self.args_sampler["sampler"] == "mrw":
            self.sampler = MRWSampler(self.data, self.args_sampler)

        self.optimizer = torch.optim.Adam(model.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        best_model = self.train()
        self.model = best_model
        return self.model

    def _train_step(self):
        self.data = self.sampler.get_subgraph("train")
        self.data.apply(lambda x: x.to(self.device))
        self.model.train()
        self.optimizer.zero_grad()
        self.model.node_classification_loss(self.data).backward()
        self.optimizer.step()

    def _test_step(self, split="val"):
        self.data = self.sampler.get_subgraph(split)
        self.data.apply(lambda x: x.to(self.device))
        self.model.eval()
        if split == "train":
            mask = self.data.train_mask
        elif split == "val":
            mask = self.data.val_mask
        else:
            mask = self.data.test_mask

        with torch.no_grad():
            logits = self.model.predict(self.data)
            loss = (
                torch.nn.NLLLoss(reduction="none")(logits[mask], self.data.y[mask]) * self.data.norm_loss[mask]
            ).sum()

        pred = logits[mask].max(1)[1]
        acc = pred.eq(self.data.y[mask]).sum().item() / mask.sum().item()
        return acc, loss


class NeighborSamplingTrainer(SampledTrainer):
    model: torch.nn.Module

    def __init__(self, args):
        super(NeighborSamplingTrainer, self).__init__(args)
        self.hidden_size = args.hidden_size
        self.sample_size = args.sample_size
        self.batch_size = args.batch_size
        self.num_workers = 4 if not hasattr(args, "num_workers") else args.num_workers
        self.eval_per_epoch = 5
        self.patience = self.patience // self.eval_per_epoch

    def fit(self, model, dataset):
        self.data = Data.from_pyg_data(dataset[0])
        self.train_loader = NeighborSampler(
            data=self.data,
            mask=self.data.train_mask,
            sizes=self.sample_size,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            shuffle=True,
        )
        self.test_loader = NeighborSampler(
            data=self.data, mask=None, sizes=[-1], batch_size=self.batch_size, shuffle=False
        )
        self.model = model.to(self.device)
        self.model.set_data_device(self.device)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        best_model = self.train()
        self.model = best_model
        acc, loss = self._test_step()
        return dict(Acc=acc["test"], ValAcc=acc["val"])

    def train(self):
        epoch_iter = tqdm(range(self.max_epoch))
        patience = 0
        max_score = 0
        min_loss = np.inf
        best_model = copy.deepcopy(self.model)
        for epoch in epoch_iter:
            self._train_step()
            if (epoch + 1) % self.eval_per_epoch == 0:
                acc, loss = self._test_step()
                train_acc = acc["train"]
                val_acc = acc["val"]
                val_loss = loss["val"]
                epoch_iter.set_description(f"Epoch: {epoch:03d}, Train: {train_acc:.4f}, Val: {val_acc:.4f}")
                if val_loss <= min_loss or val_acc >= max_score:
                    if val_loss <= min_loss:
                        best_model = copy.deepcopy(self.model)
                    min_loss = np.min((min_loss, val_loss))
                    max_score = np.max((max_score, val_acc))
                    patience = 0
                else:
                    patience += 1
                    if patience == self.patience:
                        self.model = best_model
                        epoch_iter.close()
                        break
        return best_model

    def _train_step(self):
        self.model.train()
        for target_id, n_id, adjs in self.train_loader:
            self.optimizer.zero_grad()
            x_src = self.data.x[n_id].to(self.device)
            y = self.data.y[target_id].to(self.device)
            loss = self.model.node_classification_loss(x_src, adjs, y)
            loss.backward()
            self.optimizer.step()

    def _test_step(self, split="val"):
        self.model.eval()
        masks = {"train": self.data.train_mask, "val": self.data.val_mask, "test": self.data.test_mask}
        with torch.no_grad():
            logits = self.model.inference(self.data.x, self.test_loader)

        loss = {key: F.nll_loss(logits[val], self.data.y[val]) for key, val in masks.items()}
        pred = {key: logits[val].max(1)[1] for key, val in masks.items()}
        acc = {key: pred[key].eq(self.data.y[val]).sum().item() / val.sum().item() for key, val in masks.items()}
        return acc, loss

    @classmethod
    def build_trainer_from_args(cls, args):
        return cls(args)


"""
class LayerSampledTrainer(SampledTrainer):
    def __init__(self, args):
        self.device = torch.device('cpu' if args.cpu else 'cuda')
        self.patience = args.patience
        self.max_epoch = args.max_epoch
        self.batch_size = args.batch_size

    def fit(self, model: SamplingNodeClassificationModel, dataset: Dataset):
        self.model = model.to(self.device)
        self.data = dataset.data
        self.data.apply(lambda x: x.to(self.device))
        self.sampler = LayerSampler(self.data, self.model, {})
        self.num_nodes = self.data.x.shape[0]
        self.adj_list = self.data.edge_index.detach().cpu().numpy()
        self.model.set_adj(self.adj_list, self.num_nodes)
        self.optimizer = torch.optim.Adam(
            self.model.parameters(), lr=args.lr, weight_decay=args.weight_decay
        )

        epoch_iter = tqdm(range(self.max_epoch))
        patience = 0
        best_score = 0
        best_loss = np.inf
        max_score = 0
        min_loss = np.inf
        for epoch in epoch_iter:
            self._train_step()
            train_acc, _ = self._test_step(split="train")
            val_acc, val_loss = self._test_step(split="val")
            epoch_iter.set_description(
                f"Epoch: {epoch:03d}, Train: {train_acc:.4f}, Val: {val_acc:.4f}"
            )
            if val_loss <= min_loss or val_acc >= max_score:
                if val_loss <= best_loss:  # and val_acc >= best_score:
                    best_loss = val_loss
                    best_score = val_acc
                    best_model = copy.deepcopy(self.model)
                min_loss = np.min((min_loss, val_loss))
                max_score = np.max((max_score, val_acc))
                patience = 0
            else:
                patience += 1
                if patience == self.patience:
                    self.model = best_model
                    epoch_iter.close()
                    break

    def _train_step(self):
        self.model.train()
        train_nodes = np.where(self.data.train_mask.detach().cpu().numpy())[0]
        train_labels = self.data.y.detach().cpu().numpy()
        for batch_nodes, batch_labels in get_batches(train_nodes, train_labels, batch_size=self.batch_size):
            batch_nodes = torch.LongTensor(batch_nodes)
            batch_labels = torch.LongTensor(batch_labels).to(self.device)
            sampled_x, sampled_adj, var_loss = self.sampler.sampling(self.data.x, batch_nodes)
            self.optimizer.zero_grad()
            output = self.model(sampled_x, sampled_adj)
            loss = F.nll_loss(output, batch_labels) + 0.5 * var_loss
            loss.backward()
            self.optimizer.step()

    def _test_step(self, split="val"):
        self.model.eval()
        _, mask = list(self.data(f"{split}_mask"))[0]
        test_nodes = np.where(mask.detach().cpu().numpy())[0]
        test_labels = self.data.y.detach().cpu().numpy()
        all_loss = []
        all_acc = []
        for batch_nodes, batch_labels in get_batches(test_nodes, test_labels, batch_size=self.batch_size):
            batch_nodes = torch.LongTensor(batch_nodes)
            batch_labels = torch.LongTensor(batch_labels).to(self.device)
            sampled_x, sampled_adj, var_loss = self.model.sampling(self.data.x, batch_nodes)
            with torch.no_grad():
                logits = self.model(sampled_x, sampled_adj)
                loss = F.nll_loss(logits, batch_labels)
            pred = logits.max(1)[1]
            acc = pred.eq(self.data.y[batch_nodes]).sum().item() / batch_nodes.shape[0]

            all_loss.append(loss.item())
            all_acc.append(acc)

        return np.mean(all_acc), np.mean(all_loss)
"""
