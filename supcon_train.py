from med_sclr import *
from med_sclr.supcon import SupConModel, SupConLoss

import pandas as pd
from torch.utils.data import DataLoader
import timm

CFG.cl_method = 'SimCLR'

import wandb

run = wandb.init(
    project="aml", 
    dir=OUTPUT_FOLDER,
    config={
    k:v for k, v in CFG.__dict__.items() if not k.startswith('__')}
)

clean_memory()

device = torch.device(CFG.device)

# # Load train data

# train_data = pd.read_csv(os.path.join(DATA_FOLDER, 'trainLabels.csv'))
train_data = pd.read_csv(os.path.join(DATA_FOLDER, 'trainLabels_cropped.csv')).sample(frac=1).reset_index(drop=True)


# remove all images from the csv if they are not in the folder
lst = map(lambda x: x[:-5], os.listdir(TRAIN_DATA_FOLDER))
train_data = train_data[train_data.image.isin(lst)]
# take only 100 samples from each class
train_data = train_data.groupby('level').head(CFG.samples_per_class).reset_index(drop=True)

from sklearn.metrics import f1_score as sklearn_f1
from sklearn.metrics import confusion_matrix, roc_auc_score, accuracy_score, precision_score


def train_epoch(cfg, train_loader, model, criterion, device, optimizer, scheduler, epoch):  
    model.train()

    train_loss = 0
    learning_rate_history = []
    total_len = len(train_loader)
    tk0 = tqdm(enumerate(train_loader), total=total_len)

    for step, (images, labels) in tk0:
        images = torch.cat([images[0], images[1]], dim=0)
        
        images = images.to(device)
        labels = labels.to(device)
        
        optimizer.zero_grad()
        
        bsz = labels.shape[0]
        
        # compute loss
        features = model(images)
        f1, f2 = torch.split(features, [bsz, bsz], dim=0)
        features = torch.cat([f1.unsqueeze(1), f2.unsqueeze(1)], dim=1)
        
        if CFG.cl_method == 'SupCon':
            loss = criterion(features, labels)
        elif CFG.cl_method == 'SimCLR':
            loss = criterion(features)
        else:
            raise ValueError(f"Unknown contrastive learning method: {CFG.cl_method}")
        
        loss.backward()
        optimizer.step()
        
        train_loss += loss.item()

        # Update learning rate scheduler if present
        if scheduler is not None:
            scheduler.step()
            lr = scheduler.get_last_lr()[0]
        else:
            lr = optimizer.param_groups[0]['lr']
        
        tk0.set_description(f"Epoch {epoch} training {step+1}/{total_len} [LR {lr:0.6f}] - loss: {train_loss/(step+1):.4f}")
        learning_rate_history.append(lr)

    train_loss /= total_len

    print(f'Epoch {epoch}: training loss = {train_loss:.4f}')
    return train_loss, learning_rate_history


def create_model():
    model = timm.create_model(CFG.model_name, num_classes=0, pretrained=True)
    freeze_initial_layers(model, freeze_up_to_layer=CFG.frozen_layers)
    return model.to(device)


from sklearn.manifold import TSNE
import matplotlib.colors as mcolors


## Train folds

seed_everything(CFG.seed)

train_dataset = ContrastiveLearningDataset(TRAIN_DATA_FOLDER, train_data, transform=train_transforms)
# valid_dataset = ImageTrainDataset(TRAIN_DATA_FOLDER, fold_valid_data, transforms=val_transforms)

train_loader = DataLoader(
    train_dataset,
    batch_size=CFG.batch_size,
    shuffle=True,
    num_workers=CFG.workers,
    pin_memory=True,
    drop_last=True
)

# Prepare model, optimizer, and scheduler
resnet = create_model()
model = SupConModel(resnet).to(device)
optimizer = torch.optim.AdamW(model.parameters(), lr=CFG.lr, weight_decay=CFG.weight_decay)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, eta_min=1e-6, T_max =CFG.epochs * len(train_loader))

criterion = SupConLoss()

for epoch in range(CFG.epochs):
    train_loss, train_lr = train_epoch(CFG, train_loader, model, criterion, device, optimizer, scheduler, epoch)
    scheduler.step()  # Update the learning rate scheduler at the end of each epoch

    if (epoch+1) % 3 == 0:
        torch.save(model.state_dict(), os.path.join(wandb.run.dir, f'ckpt_epoch_{epoch}.pth'))

        # plot a tsne plot of all the images using embeddings from the model
        full_dataset = ImageTrainDataset(TRAIN_DATA_FOLDER, train_data, transforms=val_transforms)
        loader = DataLoader(
            full_dataset,
            batch_size=CFG.batch_size,
            shuffle=False,
            num_workers=CFG.workers,
            pin_memory=True,
            drop_last=False,
        )

        features, targets = get_embeddings(model, loader)
        plot_tsne(features, targets, f'tsne_{epoch}.png')


wandb.finish()
