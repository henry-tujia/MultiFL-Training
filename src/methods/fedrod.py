import torch
import torch.nn.functional as F

# import logging
from src.methods.base import Base_Client, Base_Server
from src.models.init_model import Init_Model
from src.models.resnet_balance import (
    resnet_fedbalance_server_experimental as resnet_fedbalance_server,
)
from torch.multiprocessing import current_process
import numpy as np
import copy
import pandas


class Client(Base_Client):
    def __init__(self, client_dict, args):
        super().__init__(client_dict, args)
        client_dict["model_paras"].update({"KD": True})
        self.model = Init_Model(args).model.to(self.device)
        self.predictor = copy.deepcopy(self.model.fc)

        self.criterion = torch.nn.CrossEntropyLoss().to(self.device)

        self.optimizer = torch.optim.SGD(
            self.model.parameters(),
            lr=self.args.local_setting.lr,
            momentum=0.9,
            weight_decay=self.args.local_setting.wd,
            nesterov=True,
        )

        self.optimizer_prd = torch.optim.SGD(
            self.predictor.parameters(),
            lr=self.args.local_setting.lr,
            momentum=0.9,
            weight_decay=self.args.local_setting.wd,
            nesterov=True,
        )

        # self.client_infos = client_dict["client_infos"]

        # self.client_cnts = self.init_client_infos()

    def load_client_state_dict(self, server_state_dict):
        paras_old = self.model.state_dict()
        paras_new = server_state_dict

        # print(paras_new.keys())

        for key in self.upload_keys:
            paras_old[key] = paras_new[key]
            # print(key)

        self.model.load_state_dict(paras_old)

    def train(self):
        # list_for_df = []
        cdist = self.get_cdist(self.client_index)
        # train the local model
        self.model.to(self.device)
        self.model.train()
        # for name, param in self.model.named_parameters():
        #     if "local" in name :
        #         param.requires_grad = False
        #     else:
        #         param.requires_grad = True
        epoch_loss = []
        for epoch in range(self.args.local_setting.epochs):
            batch_loss = []
            for batch_idx, (images, labels) in enumerate(self.train_dataloader):
                # logging.info(images.shape)
                images, labels = images.to(self.device), labels.to(self.device)
                self.optimizer.zero_grad()
                with torch.autocast(
                    device_type=self.device.type, dtype=torch.float16, enabled=True
                ):
                    feature, log_probs = self.model(images)
                    loss_bsm = self.balanced_softmax_loss(labels, log_probs, cdist)
                    log_probs_pred = self.predictor(feature.detach())
                    loss = self.criterion(log_probs_pred + log_probs.detach(), labels)

                loss_bsm.backward()
                self.optimizer.step()
                self.optimizer_prd.zero_grad()
                loss.backward()
                self.optimizer_prd.step()

                batch_loss.append(loss.item())
            # #此处交换参数以及输出新字典
            # self.model.change_paras()
            if len(batch_loss) > 0:
                epoch_loss.append(sum(batch_loss) / len(batch_loss))
                self.logger.info(
                    "(Local Training Epoch: {} \tLoss: {:.6f}  Thread {}  Map {}".format(
                        epoch,
                        sum(epoch_loss) / len(epoch_loss),
                        current_process()._identity[0],
                        self.client_map[self.round],
                    )
                )
                ##list_for_df.append(
                # [self.round, epoch, sum(epoch_loss) / len(epoch_loss)])
        # df_save = pandas.DataFrame(list_for_df)
        # df_save.to_excel(self.args.paths.output_dir/"clients"/#"dfs"/f"{self.client_index}.xlsx")
        weights = {key: value for key, value in self.model.cpu().state_dict().items()}
        return weights, {"train_loss_epoch": epoch_loss}

    def test(self):
        self.model.to(self.device)
        self.model.eval()

        self.predictor.to(self.device)
        self.predictor.eval()

        preds = None
        labels = None
        acc = None
        with torch.no_grad():
            for batch_idx, (x, target) in enumerate(self.acc_dataloader):
                x = x.to(self.device)
                target = target.to(self.device)

                feature, log_probs = self.model(x)
                log_probs_pred = self.predictor(feature)

                # loss = self.criterion(pred, target)
                _, predicted = torch.max(log_probs_pred + log_probs, 1)
                if preds is None:
                    preds = predicted.cpu()
                    labels = target.cpu()
                else:
                    preds = torch.concat((preds, predicted.cpu()), dim=0)
                    labels = torch.concat((labels, target.cpu()), dim=0)
        for c in range(self.num_classes):
            temp_acc = (
                (
                    ((preds == labels) * (labels == c)).float()
                    / (max((labels == c).sum(), 1))
                )
                .sum()
                .cpu()
            )
            if acc is None:
                acc = temp_acc.reshape((1, -1))
            else:
                acc = torch.concat((acc, temp_acc.reshape((1, -1))), dim=0)
        weighted_acc = acc.reshape((1, -1)).mean()
        self.logger.info(
            "************* Client {} Acc = {:.2f} **************".format(
                self.client_index, weighted_acc.item()
            )
        )
        return weighted_acc

    # https://github.com/jiawei-ren/BalancedMetaSoftmax-Classification
    def balanced_softmax_loss(self, labels, logits, sample_per_class, reduction="mean"):
        """Compute the Balanced Softmax Loss between `logits` and the ground truth `labels`.
        Args:
        labels: A int tensor of size [batch].
        logits: A float tensor of size [batch, no_of_classes].
        sample_per_class: A int tensor of size [no of classes].
        reduction: string. One of "none", "mean", "sum"
        Returns:
        loss: A float tensor. Balanced Softmax Loss.
        """
        spc = sample_per_class.type_as(logits)
        spc = spc.unsqueeze(0).expand(logits.shape[0], -1)
        logits = logits + spc.log()
        loss = F.cross_entropy(input=logits, target=labels, reduction=reduction)
        return loss


class Server(Base_Server):
    def __init__(self, server_dict, args):
        super().__init__(server_dict, args)
        self.model_server = self.model_type(**server_dict["model_paras"])
        self.model = resnet_fedbalance_server(self.model_server)
        self.criterion = torch.nn.CrossEntropyLoss().to(self.device)
