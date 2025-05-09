
import os
import sys
import pathlib
sys.path.insert(0, os.path.dirname(os.path.dirname(pathlib.Path(__file__).parent.absolute())))

import argparse
import torch
import scipy
import glob
from os.path import join
import numpy as np
from tqdm import tqdm
from torch.utils.data import DataLoader

import fastmri.data.transforms as T
from models.promptmr import PromptMR
from utils import crop_submission, load_kdata, loadmat #,rotate_re
from utils import count_parameters, count_trainable_parameters, count_untrainable_parameters
# from gshift_deblur1_large_batch import GShiftNet
import os
# os.environ['CUDA_VISIBLE_DEVICES'] = '0'

def get_frames_indices_stage1(dataslice, num_slices_in_volume, num_t_in_volume=None, isT2=False):
    '''
    when we reshape t, z to one axis in preprocessing, we need to get the indices of the slices in the original t, z axis;
    then find the adjacent slices in the original z axis
    '''
    ti = dataslice//num_slices_in_volume
    zi = dataslice - ti*num_slices_in_volume

    # ti_idx_list = self._get_slice_indices(ti, num_t)
    zi_idx_list = [zi]

    if isT2: # only 3 nw in T2, so we repeat adjacent for 3 times
        ti_idx_list = [ (i+ti)%num_t_in_volume for i in range(-1,2)]
        ti_idx_list = 1*ti_idx_list[0:1] + ti_idx_list + ti_idx_list[2:3]*1
    else:
        ti_idx_list = [ (i+ti)%num_t_in_volume for i in range(-2,3)]
    # print(ti_idx_list,zi_idx_list)
    output_list = []

    for zz in zi_idx_list:
        for tt in ti_idx_list:
            output_list.append(tt*num_slices_in_volume + zz)

    return output_list

class stage1_dataset(torch.utils.data.Dataset):
    def __init__(self, fname):
        self.fname = fname
        self.kspace = load_kdata(fname)
        if 'BlackBlood' in self.fname:
            self.kspace = np.expand_dims(self.kspace, axis=0)
        else:
            pass
        # self.mask = loadmat(fname.replace('.mat','_mask.mat'))
        self.mask = loadmat(fname.replace('UnderSample','Mask').replace('kus','mask'))
        # self.mask = self.mask[list(self.mask.keys())[0]][0,:,0] #task2
        self.mask = self.mask[list(self.mask.keys())[0]][:,0] #task1
        self.num_t = self.kspace.shape[0]
        self.num_slices = self.kspace.shape[1]
        self.kspace = self.kspace.reshape(-1, self.kspace.shape[2], self.kspace.shape[3],
                                              self.kspace.shape[4]).transpose(0, 1, 3, 2)
        self.num_files = self.kspace.shape[0]
        self.ist2 = 1 if 'T2' in fname else 0
        self.maskfunc = None

    def __getitem__(self, dataslice):
        slice_idx_list = get_frames_indices_stage1(dataslice, self.num_slices, self.num_t, isT2=self.ist2)
        _input = []
        for slc_i in slice_idx_list:
            _input.append(self.kspace[slc_i])  
        _input = np.concatenate(_input, axis=0) #.transpose(0,2,1)
        kspace_torch = T.to_tensor(_input)

        masked_kspace = kspace_torch
        shape = np.array(kspace_torch.shape)
        num_cols = shape[-2]
        shape[:-3] = 1
        mask_shape = [1] * len(shape)
        mask_shape[-2] = num_cols
        # mask_torch = torch.from_numpy(self.mask.reshape(*mask_shape).astype(np.float32))
        try:
            # 你的数据处理逻辑
            mask_torch = torch.from_numpy(self.mask.reshape(*mask_shape).astype(np.float32))
            # 继续处理数据
            # return masked_kspace, mask_torch, dataslice
        except ValueError as e:
            print(f"Skipping data at index {dataslice} due to ValueError: {e}")
            return None  # 返回 None 或其他占位符来跳过此样本
        mask_torch = mask_torch.reshape(*mask_shape)
        mask_torch = mask_torch.to(torch.bool)
        return masked_kspace, mask_torch, dataslice

    def __len__(self):
        return self.num_files
    

def predict(f, num_cascades=12, model_path = '', bs1 = 1, stage=1, center_crop=False, num_works = 2, input_dir='', output_dir=''):
    # 0. config
    device = 'cuda:0'

    # 1. load model
    ## stage 1
    model1 = PromptMR(            
            num_cascades=num_cascades,  # number of unrolled iterations
            num_adj_slices=5,  # number of adjacent slices

            n_feat0=48,  # number of top-level channels for PromptUnet
            feature_dim = [72, 96, 120],
            prompt_dim = [24, 48, 72],

            sens_n_feat0=24,
            sens_feature_dim = [36, 48, 60],
            sens_prompt_dim = [12, 24, 36],
            
            no_use_ca = False,
    )

    print(f'stage1 model:\ntotal param: {count_parameters(model1)}\ntrainable param: {count_trainable_parameters(model1)}\nuntrainable param: {count_untrainable_parameters(model1)}\n##############')

    state_dict = torch.load(model_path)['state_dict']
    state_dict.pop('loss.w')
    state_dict = {k.replace('promptmr.', ''): v for k, v in state_dict.items()}
    model1.load_state_dict(state_dict)
    model1.eval()
    model1.to(device)


    ## stage 2, not released yet
    # shiftnet_model_path = '/app/model2.pt'
    # model2 = GShiftNet(past_frames=2,future_frames=2)
    # state_dict = torch.load(shiftnet_model_path)
    # model2.load_state_dict(state_dict)
    # model2.eval()
    # model2.to(device)

    # 1. predict
    with torch.no_grad():
        for ff in tqdm(f,desc='files'):
            print('-- processing --', ff)

            dataset1 = stage1_dataset(ff)
            dataloader1 = DataLoader(dataset1, batch_size=bs1, shuffle=False, num_workers=num_works, pin_memory=True, drop_last=False)
            pred_stage1 = []
                
            for masked_kspace,mask,dataslice in tqdm(dataloader1,desc='stage1'):
                if masked_kspace is None:
                    continue  # 跳过无效的数据
                # 处理数据

                bs = masked_kspace.shape[0]
                output = model1(masked_kspace.to(device),mask.to(device))
                for i in range(bs):
                    pred_stage1.append((dataslice[i],output[i:i+1]))
            pred_stage1 = torch.cat([out for _,out in sorted(pred_stage1)], dim=0).cpu()

            if stage==1: # only stage1 inference
                pred_stage2 = pred_stage1.cpu().numpy().transpose(0,2,1).reshape(dataset1.num_t, dataset1.num_slices, pred_stage1.shape[2],pred_stage1.shape[1])
            else: # two-stage inference
                raise NotImplementedError("This code is not yet released.")

            save_path = ff.replace(input_dir, output_dir).replace('UnderSample_', '')
            save_dir = os.path.dirname(save_path)
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)

            save_mat = pred_stage2.transpose(3,2,1,0) # x,y,z,t
            if center_crop:
                if 'Cine' in save_path:
                    save_mat = crop_submission(save_mat)
                else:
                    save_mat = crop_submission(save_mat,ismap=True)
            if 'BlackBlood' in save_path:
                save_mat = save_mat.squeeze()
            scipy.io.savemat(save_path, {'img4ranking':save_mat})
            print('-- saving --', save_path)

if __name__ == '__main__':
    argv = sys.argv
    parser = argparse.ArgumentParser()
    # parser.add_argument('--input', type=str, nargs='?', default='/data', help='input directory')
    # parser.add_argument('--input', type=str, nargs='?', default='/home/qitam/sdc1/MICCAI/home2/Raw_data/MICCAIChallenge2024/ChallengeData/MultiCoil', help='input directory')
    # parser.add_argument('--output', type=str, nargs='?', default='/home/qitam/sdb2/home/qiteam_project/huang/PromptMR-main/cmr_challenge_results/reproduce_promptmr_12_cascades_cmrxrecon', help='output directory')
    parser.add_argument('--input', type=str, nargs='?', default='/input', help='input directory')
    parser.add_argument('--output', type=str, nargs='?', default='/output', help='output directory')
    parser.add_argument('--model_path', type=str, nargs='?', default='promptmr-12cascades-epoch=11-step=258576.ckpt', help='model path')
    parser.add_argument('--center_crop', action='store_true', default=False, help='Enable center cropping for validation leaderboard submission')
    parser.add_argument('--stage', type=int, default=1, choices=[1, 2], help='Choose the stage: 1 or 2. Currently only stage 1 is released, since the second stage only provides marginal SSIM improvement to our PromptMR model.')
    # parser.add_argument('--evaluate_set', type=str, default='ValidationSet',choices=["ValidationSet", "TestSet"], help='Choose the evaluation set: ValidationSet or TestSet')
    parser.add_argument('--evaluate_set', type=str, default='TestSet',choices=["ValidationSet", "TestSet"], help='Choose the evaluation set: ValidationSet or TestSet')
    parser.add_argument('--task', type=str, default='Task1', choices=["Task1", "Task2", "Both"], help='Choose to inference on which type of data: Cine, Mapping or Both')
    parser.add_argument('--batch_size', type=int, default=1, help='Batch size for the model.')
    parser.add_argument('--num_works', type=int, default=2, help='num of processors to load data.')
    parser.add_argument('--num_cascades', type=int, default=12, help='num of cascades of the unrolled model.')

    args = parser.parse_args() 
    input_dir = args.input
    output_dir = args.output
    model_path = args.model_path
    center_crop = args.center_crop
    stage = args.stage
    evaluate_set = args.evaluate_set
    task = args.task
    bs1 = args.batch_size
    num_works = args.num_works
    num_cascades = args.num_cascades
    
    print("Input data store in:", input_dir)
    print("Output data store in:", output_dir)


    # get input file list
    if task == 'Task1':
        # get input file list
        f_cine = sorted([file for file in glob.glob(join(input_dir, '**/*kus*.mat'), recursive=True) if
                'Cine' in file and 'Task1' in file and evaluate_set in file])
        f_mapping = sorted([file for file in glob.glob(join(input_dir, '**/*kus*.mat'), recursive=True) if
                             'Mapping' in file and 'Task1' in file and evaluate_set in file])
        f_aorta = sorted([file for file in glob.glob(join(input_dir, '**/*kus*.mat'), recursive=True) if
                            'Aorta' in file and 'Task1' in file and evaluate_set in file])
        f_Tagging = sorted([file for file in glob.glob(join(input_dir, '**/*kus*.mat'), recursive=True) if
                             'Tagging' in file and 'Task1' in file and evaluate_set in file])
        f_BlackBlood = sorted([file for file in glob.glob(join(input_dir, '**/*kus*.mat'), recursive=True) if
                              'blackblood' in file and 'Task1' in file and evaluate_set in file])
        f_Flow2d = sorted([file for file in glob.glob(join(input_dir, '**/*kus*.mat'), recursive=True) if
                              'flow2d' in file and 'Task1' in file and evaluate_set in file])
        f = f_cine + f_mapping + f_aorta + f_Tagging + f_BlackBlood + f_Flow2d
        # f = f_BlackBlood
        print(
            f'##############\n Cine files: {len(f_cine)}\n Aorta files: {len(f_aorta)}\nMapping files: {len(f_mapping)}\nTagging files: {len(f_Tagging)}\n Total files: {len(f)}\n##############')
    elif task =="Task2":
        # get input file list
        f_cine_1 = sorted([file for file in glob.glob(join(input_dir,'**/*kus_kt*.mat'), recursive=True) if
                           'Cine' in file and evaluate_set in file])
        f_mapping_1 = sorted([file for file in glob.glob(join(input_dir, '**/*kus_kt*.mat'), recursive=True) if
                            'Mapping' in file and evaluate_set in file])
        f_aorta_1 = sorted([file for file in glob.glob(join(input_dir, '**/*kus_kt*.mat'), recursive=True) if
                            'Aorta' in file and evaluate_set in file])
        f_Tagging_1 = sorted([file for file in glob.glob(join(input_dir, '**/*kus_kt*.mat'), recursive=True) if
                             'Tagging' in file and evaluate_set in file])
        f = f_cine_1 + f_mapping_1 + f_aorta_1 + f_Tagging_1
        print(
            f'##############\n Cine files: {len(f_cine_1)}\n Aorta files: {len(f_aorta_1)}\nMapping files: {len(f_mapping_1)}\nTagging files: {len(f_Tagging_1)}\n Total files: {len(f)}\n##############')


    # main function: reconstruct and save files
    predict(f, num_cascades, model_path = model_path, bs1 = bs1, stage = stage, center_crop=center_crop, num_works=num_works, input_dir=input_dir, output_dir=output_dir)

