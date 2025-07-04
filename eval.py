import argparse
import csv
import pandas as pd
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('-g', '--gpu', type=str, default='2')
parser.add_argument('--model-file', type=str, default='./models/oneNorm/278.pth')
parser.add_argument('--save_root', type=str, default='./log_results/')
parser.add_argument('--file_name', type=str, default='Evaluation_image_level_model')
parser.add_argument('--fail_image_path', type=str, default='./fail_image_list.csv')
parser.add_argument('--model', type=str, default='IPN_V2', help='IPN_V2')
parser.add_argument('--out-stride', type=int, default=16)
parser.add_argument('--sync-bn', type=bool, default=True)
parser.add_argument('--freeze-bn', type=bool, default=False)
parser.add_argument('--epoch', type=int, default=3)
parser.add_argument('--lr', type=float, default=1e-4)
parser.add_argument('--lr-decrease-rate', type=float, default=0.9, help='ratio multiplied to initial lr')
parser.add_argument('--lr-decrease-epoch', type=int, default=1, help='interval epoch number for lr decrease')

parser.add_argument('--data-dir', default='')
parser.add_argument('--dataset', type=str, default='AIREADI')
parser.add_argument('--model-source', type=str, default='OCTA500')
parser.add_argument('--batch-size', type=int, default=1)
parser.add_argument('--image_save_metric', type=str, default='dice', choices=['dice', 'assd'])
parser.add_argument('--save_npy', action='store_true', default=False)

#test155dsad
parser.add_argument('--model-ema-rate', type=float, default=0.995)
parser.add_argument('--pseudo-label-threshold', type=float, default=0.5)
parser.add_argument('--mean-loss-calc-bound-ratio', type=float, default=0.2)

# OCTA 500 args
parser.add_argument("--in_channels", type=int, default=2, help="input channels")
parser.add_argument("--n_classes", type=int, default=5, help="class number")
parser.add_argument("--method", type=str, default="IPN_V2", help="IPN, IPN_V2")
parser.add_argument("--ava_classes", type=int, default=2, help="label channels")
parser.add_argument("--proj_map_channels", type=int, default=2, help="class number")
parser.add_argument("--get_2D_pred", type=bool, default=True, help="get 2D head")
parser.add_argument("--proj_train_ratio", type=int, default=1, help="proj_map H or W to train_size H or W ratio. Currently only supports 1 or 2")
parser.add_argument("--dc_norms", type = str, default = "NG", help="normalization for Double Conv")

parser.add_argument('--checkpoint-interval', type=int, default=400,
                    help='Save model checkpoint every K patient updates')
parser.add_argument('--resume_ckpt_path', type=str, default=None, help='Path to resume checkpoint')
parser.add_argument("--mask_optic_disc", type=bool, default=False, help="mask out the optic disc")
parser.add_argument("--run_all_success", type=bool, default=False, help="run the training and testing for all the success cases")
parser.add_argument("--save_full_level_site", type=bool, default=False, help="save the full level site")
args = parser.parse_args()

import os

os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

import os.path as osp
import numpy as np
import torch
from torch.utils.data import DataLoader, Subset
from utils.Utils import *
from utils.metrics import *
from datetime import datetime
import torch.backends.cudnn as cudnn
import random
import sys
import json
from tqdm import tqdm
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from utils.metrics import dice_coefficient, assd_coefficient

import model
from dataloaders.aireadi_dataloader import AireadiParticipantSegmentation
from dataloaders.custom_octa_transform import Custom3DTransformWeak

# test 11212
seed = 42
savefig = False
get_hd = True
model_save = True
cudnn.benchmark = False
cudnn.deterministic = True
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)

def custom_collate_fn(batch):
    collated = {}
    for key in batch[0]:
        collated[key] = [sample[key] for sample in batch]
    return collated

def patient_collate_fn_2transform(batch):
    weak_batch = custom_collate_fn([item[0] for item in batch])
    strong_batch = custom_collate_fn([item[1] for item in batch])
    return weak_batch, strong_batch


def colorize_segmentation(seg_idx):
    """
    Convert a single-channel integer segmentation map (H,W)
    into an RGB color image (H,W,3).

    seg_idx: Torch or NumPy array of shape [H, W] with values in [0..4].
    """
    if isinstance(seg_idx, torch.Tensor):
        seg_idx = seg_idx.cpu().numpy()

    H, W = seg_idx.shape
    seg_color = np.zeros((H, W, 3), dtype=np.uint8)

    # Define color map: {class_index: (R, G, B)}
    color_map = {
        0: (0, 0, 0),         # background -> black
        1: (128, 128, 128),   # capillary  -> gray
        2: (255, 0, 0),       # artery     -> red
        3: (0, 0, 255),       # vein       -> blue
        4: (0, 255, 0),       # FAZ        -> green
    }

    for cls_id, (r, g, b) in color_map.items():
        mask = (seg_idx == cls_id)
        seg_color[mask] = [r, g, b]

    return seg_color


def save_segmentation_png(gt_seg, pred_seg, map_s, sample, score_per_class, save_dir='.', prefix='sample', mode='train', save_npy=True):
    if isinstance(gt_seg, torch.Tensor):
        gt_seg = gt_seg.cpu().numpy()
        if gt_seg.ndim == 4:
            gt_seg = gt_seg.squeeze(1)
    if isinstance(pred_seg, torch.Tensor):
        pred_seg = pred_seg.cpu().numpy()
    if isinstance(map_s, torch.Tensor):
        map_s = map_s.cpu().numpy()

    N, H, W = gt_seg.shape

    fig, axes = plt.subplots(nrows=N, ncols=3, figsize=(10, 5 * N))

    if N == 1:
        axes = np.expand_dims(axes, axis=0)

    if map_s.ndim == 4:
        proj_imgs = map_s[:, 1]
    else:
        raise ValueError("Projection map must be either [N, C, H, W] or [N, H, W].")

    if mode == 'train':
        label_name = 'pseudo_label'
    else:
        label_name = 'GT_label'

    for i in range(N):
        if save_npy:
            np.save(osp.join(save_dir, f"{prefix}_pred_{i}.npy"), pred_seg[i])
        proj_img = proj_imgs[i]
        proj_color = (np.stack([proj_img] * 3, axis=-1) * 255).astype(np.uint8)

        label_img = colorize_segmentation(gt_seg[i])
        pred_img = colorize_segmentation(pred_seg[i])

        axes[i, 0].imshow(proj_color)
        axes[i, 0].set_title(f"patient_id:{sample['participant_id']}, laterality:{sample['laterality']}\n manufacturer:{sample['manufacturer']}, anatomical:{sample['anatomical']}, \n region_size:{sample['region_size']}")
        # axes[i, 0].axis("off")

        axes[i, 1].imshow(label_img)
        axes[i, 1].set_title(label_name)
        # axes[i, 1].axis('off')

        axes[i, 2].imshow(pred_img)
        axes[i, 2].set_title(f"model Pred: dice: {score_per_class[0]:.4f}, \n{score_per_class[1]:.4f}, \n{score_per_class[2]:.4f}, \n{score_per_class[3]:.4f}, \n{score_per_class[4]:.4f}, \n{score_per_class[5]:.4f}")
        # axes[i, 2].axis('off')

    plt.tight_layout()

    combined_save_path = osp.join(save_dir, f"{prefix}.png")
    plt.savefig(combined_save_path)
    plt.close(fig)


def eval_final(args, model, data_loader, current_epoch=None, step=None, mode='Teacher'):
    model.eval()

    manufacturer_gt = []
    manufacturer_pred = []
    manufacturer_prob = []

    anatomical_gt = []
    anatomical_pred = []
    anatomical_prob = []

    region_size_gt = []
    region_size_pred = []
    region_size_prob = []

    laterality_gt = []
    laterality_pred = []
    laterality_prob = []

    per_class_dice_scores_all = {c_cls: [] for c_cls in range(6)}

    per_class_dice_scores_1 = {c_cls: [] for c_cls in range(6)}

    per_class_dice_scores_4 = {c_cls: [] for c_cls in range(6)}

    per_class_dice_scores_7 = {c_cls: [] for c_cls in range(6)}

    per_class_zeiss_disc_6x6_all = {c_cls: [] for c_cls in range(6)}

    per_class_zeiss_disc_6x6_1 = {c_cls: [] for c_cls in range(6)}

    per_class_zeiss_disc_6x6_4 = {c_cls: [] for c_cls in range(6)}

    per_class_zeiss_disc_6x6_7 = {c_cls: [] for c_cls in range(6)}

    per_class_triton_macula_12x12_all = {c_cls: [] for c_cls in range(6)}

    per_class_triton_macula_12x12_1 = {c_cls: [] for c_cls in range(6)}

    per_class_triton_macula_12x12_4 = {c_cls: [] for c_cls in range(6)}

    per_class_triton_macula_12x12_7 = {c_cls: [] for c_cls in range(6)}

    per_class_macula_6x6_all = {c_cls: [] for c_cls in range(6)}

    per_class_macula_6x6_1 = {c_cls: [] for c_cls in range(6)}

    per_class_macula_6x6_4 = {c_cls: [] for c_cls in range(6)}

    per_class_macula_6x6_7 = {c_cls: [] for c_cls in range(6)}

    per_class_topcon_maestro2_all = {c_cls: [] for c_cls in range(6)}

    per_class_topcon_maestro2_1 = {c_cls: [] for c_cls in range(6)}

    per_class_topcon_maestro2_4 = {c_cls: [] for c_cls in range(6)}

    per_class_topcon_maestro2_7 = {c_cls: [] for c_cls in range(6)}

    per_class_topcon_triton_all = {c_cls: [] for c_cls in range(6)}

    per_class_topcon_triton_1 = {c_cls: [] for c_cls in range(6)}

    per_class_topcon_triton_4 = {c_cls: [] for c_cls in range(6)}

    per_class_topcon_triton_7 = {c_cls: [] for c_cls in range(6)}

    per_class_zeiss_cirrus_all = {c_cls: [] for c_cls in range(6)}

    per_class_zeiss_cirrus_1 = {c_cls: [] for c_cls in range(6)}

    per_class_zeiss_cirrus_4 = {c_cls: [] for c_cls in range(6)}

    per_class_zeiss_cirrus_7 = {c_cls: [] for c_cls in range(6)}

    save_dir = osp.join(args.out, f"eval_final")

    wrong_disc_faz_count = 0
    total_optic_disc = 0

    with torch.no_grad():
        for batch_idx, samples in tqdm(enumerate(data_loader), total=len(data_loader), desc="Evaluating"):
            batch_size = len(samples["samples"])
            assert batch_size == args.batch_size
            for i in range(batch_size):
                for idx, sample in enumerate(samples['samples'][i]):

                    data = sample['image'].squeeze(1)
                    proj_map = sample['proj_map'].squeeze(1)

                    manufacturer_labels = torch.tensor(sample['manufacturer']).long().cuda()
                    anatomical_labels = torch.tensor(sample['anatomical']).long().cuda()
                    region_size_labels = torch.tensor(sample['region_size']).long().cuda()
                    laterality_labels = torch.tensor(sample['laterality']).long().cuda()
                    img_name = sample['img_name']

                    row = sample['row']
                    manufacturer_val = row['manufacturer']
                    anatomic_region_val = row['anatomic_region']
                    model_name = row['manufacturers_model_name']
                    participant_id = str(row['participant_id'])
                    laterality_val = row['laterality']

                    data = data.cuda()
                    proj_map = proj_map.cuda()

                    cavf_pred3D, _, manufacturer_logits, anatomical_logits, region_size_logits, laterality_logits, _ = model(data, proj_map)
                    cavf_pred3D = torch.softmax(cavf_pred3D, dim=1)

                    gt_seg = sample['data_label']
                    if isinstance(gt_seg, np.ndarray):
                        gt_seg = torch.from_numpy(gt_seg).to(data.device)

                    if gt_seg.dim() == 3:
                        gt_seg = gt_seg.unsqueeze(0)

                    if gt_seg.dim() == 4 and gt_seg.shape[1] == 5:
                        gt_seg = torch.argmax(gt_seg, dim=1)
                    pred_seg = torch.argmax(cavf_pred3D, dim=1)

                    # for i in range(anatomical_labels.shape[0]):
                    if args.mask_optic_disc and anatomical_labels.item() == 1:
                            gt_seg = torch.where(gt_seg == 4, torch.zeros_like(gt_seg), gt_seg)

                    # gt_seg = torch.where(gt_seg == 1, torch.zeros_like(gt_seg), gt_seg)
                    # pred_seg = torch.where(pred_seg == 1, torch.zeros_like(pred_seg), pred_seg)

                    image_save_path = osp.join(args.out, f"images")
                    if not osp.exists(image_save_path):
                        os.makedirs(image_save_path)


                    dice_per_class = {}
                    assd_per_class = {}
                    for i in range(pred_seg.size(0)):
                        for c_cls in range(6):
                            if c_cls < 5:
                                pred_mask = (pred_seg[i] == c_cls).float()
                                gt_mask   = (gt_seg[i]   == c_cls).float()
                            else:
                                pred_mask = ((pred_seg[i] == 0) | (pred_seg[i] == 1)).float()
                                gt_mask = ((gt_seg[i] == 0) | (gt_seg[i] == 1)).float()
                                # Since we only examine A, V, F. We combine the Bg and Capillary masks together as a new class.

                            if c_cls == 4:
                                # pred_mask, gt_mask are [H, W] at this point
                                H, W = pred_mask.shape
                                y1, y2 = int(0.25 * H), int(0.75 * H)
                                x1, x2 = int(0.25 * W), int(0.75 * W)
                                # we only care about the center region.

                                # Slice down to center region
                                pred_mask = pred_mask[y1:y2, x1:x2]
                                gt_mask = gt_mask[..., y1:y2, x1:x2]

                            # print(f"pred_mask shape: {pred_mask.shape}, gt_mask shape: {gt_mask.shape}")
                            dice = dice_coefficient(pred_mask, gt_mask)

                            pred_mask_np = (pred_mask.cpu().numpy() > 0.5).astype(np.bool_)
                            gt_mask_np   = (gt_mask.cpu().numpy() > 0.5).astype(np.bool_)
                            if args.save_npy:
                                np.save(osp.join(image_save_path, f"{participant_id}_{i}_{idx}_pred_mask_{c_cls}.npy"), pred_mask_np)
                                np.save(osp.join(image_save_path, f"{participant_id}_{i}_{idx}_gt_mask_{c_cls}.npy"), gt_mask_np)
                            if len(gt_mask_np.shape) == 2:
                                pass
                            elif len(gt_mask_np.shape) == 3:
                                gt_mask_np = gt_mask_np[0]
                            else:
                                raise ValueError(f"Invalid gt_mask_np shape: {gt_mask_np.shape}")
                            assd = assd_coefficient(pred_mask_np, gt_mask_np)
                            print(f"Participant ID: {participant_id}, Image ID: {i}, Class: {c_cls}, Dice: {dice:.4f}, ASSD: {assd:.4f}")

                            dice_per_class[c_cls] = dice
                            assd_per_class[c_cls] = assd

                        row_dict = {
                            "participant_id": participant_id,
                            "manufacturer": manufacturer_val,
                            "manufacturers_model_name": model_name,
                            "anatomic_region": anatomic_region_val,
                            "laterality": laterality_val,
                            "dice_0": dice_per_class[0],
                            "dice_1": dice_per_class[1],
                            "dice_2": dice_per_class[2],
                            "dice_3": dice_per_class[3],
                            "dice_4": dice_per_class[4],
                            "dice_5": dice_per_class[5],
                            "assd_0": assd_per_class[0],
                            "assd_1": assd_per_class[1],
                            "assd_2": assd_per_class[2],
                            "assd_3": assd_per_class[3],
                            "assd_4": assd_per_class[4],
                            "assd_5": assd_per_class[5],
                            "img_name": img_name
                        }

                        if anatomic_region_val == 'Optic Disc, 6 x 6':
                            total_optic_disc += 1
                            if dice_per_class[4] < 1e-6:
                                wrong_disc_faz_count += 1

                        # if anatomic_region_val != 'Optic Disc, 6 x 6':
                        per_class_dice_scores_all[c_cls].append(row_dict)

                        if str(participant_id).startswith('1'):
                            per_class_dice_scores_1[c_cls].append(row_dict)
                        if str(participant_id).startswith('4'):
                            per_class_dice_scores_4[c_cls].append(row_dict)
                        if str(participant_id).startswith('7'):
                            per_class_dice_scores_7[c_cls].append(row_dict)

                        if manufacturer_val == 'Zeiss' and anatomic_region_val == 'Optic Disc, 6 x 6':
                            per_class_zeiss_disc_6x6_all[c_cls].append(row_dict)
                            if str(participant_id).startswith('1'):
                                per_class_zeiss_disc_6x6_1[c_cls].append(row_dict)
                            if str(participant_id).startswith('4'):
                                per_class_zeiss_disc_6x6_4[c_cls].append(row_dict)
                            if str(participant_id).startswith('7'):
                                per_class_zeiss_disc_6x6_7[c_cls].append(row_dict)

                        # Triton Macula, 12 x 12.
                        if model_name == 'Triton' and anatomic_region_val == 'Macula, 12 x 12':
                            per_class_triton_macula_12x12_all[c_cls].append(row_dict)
                            if str(participant_id).startswith('1'):
                                per_class_triton_macula_12x12_1[c_cls].append(row_dict)
                            if str(participant_id).startswith('4'):
                                per_class_triton_macula_12x12_4[c_cls].append(row_dict)
                            if str(participant_id).startswith('7'):
                                per_class_triton_macula_12x12_7[c_cls].append(row_dict)

                        # Macula, 6 x 6.
                        if anatomic_region_val == 'Macula, 6 x 6' and model_name == 'Cirrus':
                            per_class_macula_6x6_all[c_cls].append(row_dict)
                            if str(participant_id).startswith('1'):
                                per_class_macula_6x6_1[c_cls].append(row_dict)
                            if str(participant_id).startswith('4'):
                                per_class_macula_6x6_4[c_cls].append(row_dict)
                            if str(participant_id).startswith('7'):
                                per_class_macula_6x6_7[c_cls].append(row_dict)

                        # Topcon: for maestro2.
                        if manufacturer_val == 'Topcon' and model_name == 'Maestro2':
                            per_class_topcon_maestro2_all[c_cls].append(row_dict)
                            if str(participant_id).startswith('1'):
                                per_class_topcon_maestro2_1[c_cls].append(row_dict)
                            if str(participant_id).startswith('4'):
                                per_class_topcon_maestro2_4[c_cls].append(row_dict)
                            if str(participant_id).startswith('7'):
                                per_class_topcon_maestro2_7[c_cls].append(row_dict)

                        # Topcon: for Triton.
                        if manufacturer_val == 'Topcon' and model_name ==  'Triton':
                            per_class_topcon_triton_all[c_cls].append(row_dict)
                            if str(participant_id).startswith('1'):
                                per_class_topcon_triton_1[c_cls].append(row_dict)
                            if str(participant_id).startswith('4'):
                                per_class_topcon_triton_4[c_cls].append(row_dict)
                            if str(participant_id).startswith('7'):
                                per_class_topcon_triton_7[c_cls].append(row_dict)

                        # Zeiss Cirrus.
                        if manufacturer_val == 'Zeiss' and model_name == 'Cirrus':
                            per_class_zeiss_cirrus_all[c_cls].append(row_dict)
                            if str(participant_id).startswith('1'):
                                per_class_zeiss_cirrus_1[c_cls].append(row_dict)
                            if str(participant_id).startswith('4'):
                                per_class_zeiss_cirrus_4[c_cls].append(row_dict)
                            if str(participant_id).startswith('7'):
                                per_class_zeiss_cirrus_7[c_cls].append(row_dict)

                    if args.image_save_metric == 'dice':
                        score_per_class = dice_per_class
                    elif args.image_save_metric == 'assd':
                        score_per_class = assd_per_class
                    else:
                        raise ValueError(f"Invalid image save metric: {args.image_save_metric}")
                    save_segmentation_png(gt_seg, pred_seg, proj_map, sample, score_per_class, save_dir=image_save_path, prefix=f'Evaluation_batch_idx{batch_idx}_{i}_{idx}', mode='eval', save_npy=args.save_npy)

                    manufacturer_probs = torch.softmax(manufacturer_logits, dim=1)
                    anatomical_probs   = torch.softmax(anatomical_logits, dim=1)
                    region_size_probs  = torch.softmax(region_size_logits, dim=1)
                    laterality_probs   = torch.softmax(laterality_logits, dim=1)

                    manufacturer_preds = torch.argmax(manufacturer_probs, dim=1)
                    anatomical_preds   = torch.argmax(anatomical_probs, dim=1)
                    region_size_preds  = torch.argmax(region_size_probs, dim=1)
                    laterality_preds   = torch.argmax(laterality_probs, dim=1)

                    manufacturer_gt.append(manufacturer_labels.cpu().numpy())
                    manufacturer_pred.append(manufacturer_preds.cpu().numpy())
                    manufacturer_prob.append(manufacturer_probs.cpu().numpy())

                    anatomical_gt.append(anatomical_labels.cpu().numpy())
                    anatomical_pred.append(anatomical_preds.cpu().numpy())
                    anatomical_prob.append(anatomical_probs.cpu().numpy())

                    region_size_gt.append(region_size_labels.cpu().numpy())
                    region_size_pred.append(region_size_preds.cpu().numpy())
                    region_size_prob.append(region_size_probs.cpu().numpy())

                    laterality_gt.append(laterality_labels.cpu().numpy())
                    laterality_pred.append(laterality_preds.cpu().numpy())
                    laterality_prob.append(laterality_probs.cpu().numpy())

    manufacturer_gt = np.array(manufacturer_gt)
    manufacturer_pred = np.array(manufacturer_pred)
    manufacturer_prob = np.array(manufacturer_prob)

    anatomical_gt = np.array(anatomical_gt)
    anatomical_pred = np.array(anatomical_pred)
    anatomical_prob = np.array(anatomical_prob)

    region_size_gt = np.array(region_size_gt)
    region_size_pred = np.array(region_size_pred)
    region_size_prob = np.array(region_size_prob)

    laterality_gt = np.array(laterality_gt)
    laterality_pred = np.array(laterality_pred)
    laterality_prob = np.array(laterality_prob)

    metrics = {}

    metrics['manufacturer_accuracy'] = accuracy_score(manufacturer_gt, manufacturer_pred)
    metrics['manufacturer_f1'] = f1_score(manufacturer_gt, manufacturer_pred, average='macro')
    try:
        metrics['manufacturer_auc'] = roc_auc_score(manufacturer_gt, manufacturer_prob, multi_class='ovr')
    except Exception as e:
        metrics['manufacturer_auc'] = np.nan
        print("Manufacturer AUC calculation error:", e)

    metrics['anatomical_accuracy'] = accuracy_score(anatomical_gt, anatomical_pred)
    metrics['anatomical_f1'] = f1_score(anatomical_gt, anatomical_pred, average='macro')
    try:
        metrics['anatomical_auc'] = roc_auc_score(anatomical_gt, anatomical_prob, multi_class='ovr')
    except Exception as e:
        metrics['anatomical_auc'] = np.nan
        print("Anatomical AUC calculation error:", e)

    metrics['region_size_accuracy'] = accuracy_score(region_size_gt, region_size_pred)
    metrics['region_size_f1'] = f1_score(region_size_gt, region_size_pred, average='macro')
    try:
        metrics['region_size_auc'] = roc_auc_score(region_size_gt, region_size_prob, multi_class='ovr')
    except Exception as e:
        metrics['region_size_auc'] = np.nan
        print("Region Size AUC calculation error:", e)

    metrics['laterality_accuracy'] = accuracy_score(laterality_gt, laterality_pred)
    metrics['laterality_f1'] = f1_score(laterality_gt, laterality_pred, average='macro')
    try:
        metrics['laterality_auc'] = roc_auc_score(laterality_gt, laterality_prob, multi_class='ovr')
    except Exception as e:
        metrics['laterality_auc'] = np.nan
        print("Laterality AUC calculation error:", e)


    metrics['per_class_dice_scores_all'] = per_class_dice_scores_all

    metrics['per_class_dice_scores_1'] = per_class_dice_scores_1

    metrics['per_class_dice_scores_4'] = per_class_dice_scores_4

    metrics['per_class_dice_scores_7'] = per_class_dice_scores_7

    # Add subset segmentation scores.
    metrics['per_class_zeiss_disc_6x6_all'] = per_class_zeiss_disc_6x6_all

    metrics['per_class_triton_macula_12x12_all'] = per_class_triton_macula_12x12_all

    metrics['per_class_macula_6x6_all'] = per_class_macula_6x6_all

    metrics['per_class_topcon_maestro2_all'] = per_class_topcon_maestro2_all

    metrics['per_class_topcon_triton_all'] = per_class_topcon_triton_all

    metrics['per_class_zeiss_cirrus_all'] = per_class_zeiss_cirrus_all


    if args.save_full_level_site:
        metrics['per_class_zeiss_cirrus_1'] = per_class_zeiss_cirrus_1

        metrics['per_class_zeiss_cirrus_4'] = per_class_zeiss_cirrus_4

        metrics['per_class_zeiss_cirrus_7'] = per_class_zeiss_cirrus_7

        metrics['per_class_zeiss_cirrus_1'] = per_class_zeiss_cirrus_1

        metrics['per_class_zeiss_cirrus_4'] = per_class_zeiss_cirrus_4

        metrics['per_class_zeiss_cirrus_7'] = per_class_zeiss_cirrus_7

        metrics['per_class_topcon_maestro2_1'] = per_class_topcon_maestro2_1

        metrics['per_class_topcon_maestro2_4'] = per_class_topcon_maestro2_4

        metrics['per_class_topcon_maestro2_7'] = per_class_topcon_maestro2_7

        metrics['per_class_macula_6x6_1'] = per_class_macula_6x6_1

        metrics['per_class_macula_6x6_4'] = per_class_macula_6x6_4

        metrics['per_class_macula_6x6_7'] = per_class_macula_6x6_7

        metrics['per_class_triton_macula_12x12_1'] = per_class_triton_macula_12x12_1

        metrics['per_class_triton_macula_12x12_4'] = per_class_triton_macula_12x12_4

        metrics['per_class_triton_macula_12x12_7'] = per_class_triton_macula_12x12_7

        metrics['per_class_zeiss_disc_6x6_1'] = per_class_zeiss_disc_6x6_1

        metrics['per_class_zeiss_disc_6x6_4'] = per_class_zeiss_disc_6x6_4

        metrics['per_class_zeiss_disc_6x6_7'] = per_class_zeiss_disc_6x6_7

    save_metrics_to_files(metrics, save_dir)

    print("Wrong disc Faz count:", wrong_disc_faz_count)
    print("Total optic disc count:", total_optic_disc)


def save_metrics_to_files(metrics, output_dir):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    summary_lines = []
    for key, value in metrics.items():
        if not isinstance(value, (dict, list)):
            summary_lines.append(f"{key}: {value}")
    summary_text = "\n".join(summary_lines)
    txt_path = os.path.join(output_dir, "metrics_summary.txt")
    with open(txt_path, "w") as f:
        f.write(summary_text)
    print(f"Saved overall metrics summary to {txt_path}")

    json_path = os.path.join(output_dir, "metrics.json")
    with open(json_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print(f"Saved full metrics dictionary to {json_path}")

    for subset_key in metrics:
        if subset_key.startswith("per_class_"):
            csv_filename = f"{subset_key}.csv"
            write_csv_from_list(metrics[subset_key], csv_filename, output_dir)


def write_csv_from_list(dict_list, filename, output_dir):
    all_rows = []
    for cls in sorted(dict_list.keys()):
        all_rows.extend(dict_list[cls])
    if not all_rows:
        return
    keys = ["participant_id", "manufacturer", "manufacturers_model_name", "anatomic_region", "laterality",
            "dice_0", "dice_1", "dice_2", "dice_3", "dice_4", "dice_5",
            "assd_0", "assd_1", "assd_2", "assd_3", "assd_4", "assd_5", "img_name"]
    csv_path = os.path.join(output_dir, filename)
    with open(csv_path, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=keys)
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)
    print(f"Saved {filename} to {csv_path}")


def summarize_csv_metrics(input_dir, output_txt):
    # These lists will hold the summary strings.
    summary_lines_both = []
    summary_lines_mean = []
    summary_lines_std = []

    # Get all CSV files that start with "per_class_" and end with ".csv"
    csv_files = sorted([f for f in os.listdir(input_dir) if f.startswith("per_class_") and f.endswith(".csv")])
    if not csv_files:
        print("No CSV files found in", input_dir)
        return

    for csv_file in csv_files:
        csv_path = os.path.join(input_dir, csv_file)
        df = pd.read_csv(csv_path)
        header_line = f"Summary for {csv_file}:"
        summary_lines_both.append(header_line)
        summary_lines_mean.append(header_line)
        summary_lines_std.append(header_line)
        for cls in range(6):  # now 6 classes (0 to 5)
            dice_col = f"dice_{cls}"
            assd_col = f"assd_{cls}"
            if dice_col in df.columns and assd_col in df.columns:

                if cls == 4:
                    df = df[df["anatomic_region"] != "Optic Disc, 6 x 6"]

                dice_mean = df[dice_col].mean() * 100
                dice_std = df[dice_col].std() * 100
                assd_mean = df[assd_col].mean()
                assd_std = df[assd_col].std()
                summary_lines_both.append(
                    f"  Class {cls}: Dice: {dice_mean:.1f} ± {dice_std:.2f}, ASSD: {assd_mean:.2f} ± {assd_std:.2f}"
                )
                summary_lines_mean.append(
                    f"  Class {cls}: Dice: {dice_mean:.4f}, ASSD: {assd_mean:.4f}"
                )
                summary_lines_std.append(
                    f"  Class {cls}: Dice: {dice_std:.4f}, ASSD: {assd_std:.4f}"
                )
            else:
                msg = f"  Class {cls}: Columns {dice_col} or {assd_col} not found."
                summary_lines_both.append(msg)
                summary_lines_mean.append(msg)
                summary_lines_std.append(msg)
        # Append a blank line between files.
        summary_lines_both.append("")
        summary_lines_mean.append("")
        summary_lines_std.append("")

    # Determine the output directory and create three file names.
    base_dir = os.path.dirname(output_txt)
    both_path = os.path.join(base_dir, "metrics_summary_both.txt")
    mean_path = os.path.join(base_dir, "metrics_summary_mean.txt")
    std_path  = os.path.join(base_dir, "metrics_summary_std.txt")

    with open(both_path, "w") as f:
        f.write("\n".join(summary_lines_both))
    with open(mean_path, "w") as f:
        f.write("\n".join(summary_lines_mean))
    with open(std_path, "w") as f:
        f.write("\n".join(summary_lines_std))
    print(f"Summary saved to:\n{both_path}\n{mean_path}\n{std_path}")


def main():
    now = datetime.now()
    args.out = osp.join(args.save_root, now.strftime('%Y%m%d_%H%M%S') + args.file_name)
    if not osp.exists(args.out):
        os.makedirs(args.out)
    args.out_file = open(osp.join(args.out, now.strftime('%Y%m%d')+'.txt'), 'w')
    args.out_file.write(' '.join(sys.argv) + '\n')
    args.out_file.flush()

    args_dict = vars(args).copy()
    if "out_file" in args_dict:
        del args_dict["out_file"]

    json_path = osp.join(args.out, "args.json")
    with open(json_path, "w") as f:
        json.dump(args_dict, f, indent=2)

    custom_transform_weak = Custom3DTransformWeak()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    roi_target_depth = 800

    dataset_test = AireadiParticipantSegmentation(
        root=args.data_dir,
        roi=roi_target_depth,
        device=device,
        mode='test',
        transform=custom_transform_weak,
        all_success=args.run_all_success,
        fail_image_path=args.fail_image_path,
    )

    test_loader = DataLoader(dataset_test, batch_size=args.batch_size, shuffle=False, num_workers=4, collate_fn=custom_collate_fn)

    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
    device_ids = [int(x) for x in args.gpu.split(',')]

    if args.method == "IPN":
        model_s = model.IPN(in_channels=args.in_channels, n_classes=args.n_classes)
        model_t = model.IPN(in_channels=args.in_channels, n_classes=args.n_classes)
    if args.method == "IPN_V2":
        model_s = model.IPNV2_with_proj_map(in_channels=args.in_channels, n_classes=args.n_classes,
                                        proj_map_in_channels=args.proj_map_channels,
                                        ava_classes=args.ava_classes, get_2D_pred=args.get_2D_pred,
                                        proj_vol_ratio=args.proj_train_ratio, dc_norms=args.dc_norms)
        model_t = model.IPNV2_with_proj_map(in_channels=args.in_channels, n_classes=args.n_classes,
                                        proj_map_in_channels=args.proj_map_channels,
                                        ava_classes=args.ava_classes, get_2D_pred=args.get_2D_pred,
                                        proj_vol_ratio=args.proj_train_ratio, dc_norms=args.dc_norms)

    if torch.cuda.is_available():
        model_s = model_s.cuda()
        model_t = model_t.cuda()
    log_str = '==> Loading %s model file: %s' % (model_s.__class__.__name__, args.model_file)
    print(log_str)
    args.out_file.write(log_str + '\n')
    args.out_file.flush()
    checkpoint = torch.load(args.model_file)
    model_s.load_state_dict(checkpoint, strict=False)
    model_t.load_state_dict(checkpoint, strict=False)

    if len(device_ids) > 1:
        print("Using multiple GPUs")
        print(device_ids)
        model_s = torch.nn.DataParallel(model_s, device_ids=device_ids)
        model_t = torch.nn.DataParallel(model_t, device_ids=device_ids)

    model_s.train()
    model_t.train()
    for param in model_t.parameters():
        param.requires_grad = False

    global last_checkpoint_path
    last_checkpoint_path = None

    if args.resume_ckpt_path is not None:
        resume_ckpt = osp.join(args.out, args.resume_ckpt_path)
        if osp.exists(resume_ckpt):
            print("Resuming training from checkpoint:", resume_ckpt)
            checkpoint = torch.load(resume_ckpt)
            model_s.load_state_dict(checkpoint['model_state_dict'])
            model_t.load_state_dict(checkpoint['model_state_dict'])

    eval_final(args, model_t, test_loader, mode='Final')

    csv_output_dir = osp.join(args.out, "eval_final")
    summary_txt_path = osp.join(args.out, "metrics_summary.txt")
    summarize_csv_metrics(csv_output_dir, summary_txt_path)


if __name__ == '__main__':
    main()

