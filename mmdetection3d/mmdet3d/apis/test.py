# Copyright (c) OpenMMLab. All rights reserved.
from os import path as osp

import time
from PIL import Image
import mmcv
import torch
import numpy as np
from mmcv.image import tensor2imgs
import cv2
import os
import copy

from sklearn import linear_model
from mmdet3d.models import (Base3DDetector, Base3DSegmentor,
                            SingleStageMono3DDetector)

from utils.utils import checkWarning, center2ImageBev, lidar2Bev, Sort, SortCustom, detection2Bev, from_latlon, bevPoints, drawBbox, transformImg, bevPoints_tracking, load_kitti_tracking_label, load_kitti_tracking_calib, Cam2LidarBev_tracking, makeForecastDict, filterUpdatedIds, forecastTest, find_files, cal_proj_matrix, cal_proj_matrix_raw, load_img, load_lidar, project_lidar2img, add_square_feature

from lib.core.general import non_max_suppression, scale_coords
from lib.utils import plot_one_box,show_seg_result
from ..core.evaluation.kitti_utils.rotate_iou import rotate_iou_gpu_eval

import sys
sys.path.append("../tools/PECNet/utils/")
from social_utils_add import *

pre_LANE_OURS = [np.array([[1, 1.5], [2, 1.5], [3, 1.5]]), np.array([[1, -1.5], [2, -1.5], [3, -1.5]])]
pre_LANE_RIGHT = [pre_LANE_OURS[1]]
pre_LANE_LEFT = [pre_LANE_OURS[0]]
pre_line_X_right = None
pre_line_Y_right = None
pre_line_X_left = None
pre_line_Y_left = None

def single_gpu_test(model,
                    model_2d,
                    model_forecast,
                    hyper_params,
                    data_loader,
                    args,
                    cfg,
                    show=False,
                    out_dir=None,
                    show_score_thr=0.3
                    ):
    """Test model with single gpu.

    This method tests model with single gpu and gives the 'show' option.
    By setting ``show=True``, it saves the visualization results under
    ``out_dir``.

    Args:
        model (nn.Module): Model to be tested.
        data_loader (nn.Dataloader): Pytorch data loader.
        show (bool, optional): Whether to save viualization results.
            Default: True.
        out_dir (str, optional): The path to save visualization results.
            Default: None.

    Returns:
        list[dict]: The prediction results.
    """
    global pre_LANE_OURS, pre_LANE_LEFT, pre_LANE_RIGHT
    global lane_xy, pre_line_X_right, pre_line_Y_right, pre_line_X_left, pre_line_Y_left
    model.eval()
    model_2d.eval()
    names = model_2d.module.names if hasattr(model_2d, 'module') else model_2d.names
    colors = [[np.random.randint(0, 255) for _ in range(3)] for _ in range(len(names))]

    results = []
    bev_results = []
    dataset = data_loader.dataset
    prog_bar = mmcv.ProgressBar(len(dataset))

    
    label = load_kitti_tracking_label('/opt/ml/kitti1/label_0000.txt')
    calib = load_kitti_tracking_calib('/opt/ml/kitti1/testing/calib/calib_0000.txt')
    R_vc = calib['R0_rect']
    T_vc = calib['Tr_velo_to_cam']
    P_ = calib['P2']
    v2p_matrix = P_@R_vc@T_vc

    mot_tracker = SortCustom(v2p_matrix = v2p_matrix,
                            iou_threshold = 0.4,
                            max_age=args.max_age, 
                            min_hits=args.min_hits,
                            centerpoint_threshold=args.centerpoint_threshold) #create instance of the SORT tracker
    
    bbox_2d_txt = open('/opt/ml/bbox_2d.txt', 'w')
    bbox_3d_txt = open('/opt/ml/bbox_3d.txt', 'w')
    forecast_dict = {}
    filtered_updated_ids = []
    predicted_updated_ids = []
    prev_forecast = np.empty((0, 0, 0))
    oxt_dict = {}
    # with open('/opt/ml/kitti_testing_13/oxt_0013.txt', 'r') as oxt_file:
    #     oxt_datas = oxt_file.readlines()

    if not os.path.exists('/opt/ml/tracking'):
        os.makedirs('/opt/ml/tracking')

    global lane_t_num
    lane_t_num = 0
    with open('/opt/ml/tracking/tracking.txt', 'w') as tracking_file:
        print(f'if using cv2 to draw points -> x,y order : cv2.line(..., pt1=(x,y), pt2=(x,y), ...)', file=tracking_file)
        print(f'frame, tracking_id, x(garo), y(sero), class_id', file=tracking_file)
        for current_frame, data in enumerate(data_loader):
            """
            YOLOP start
            """
            velodyne_path = dataset[current_frame]['img_metas'][0].data['pts_filename']
            img_path = velodyne_path.replace('velodyne', 'image_2').replace('bin', 'png')
            
            img = Image.open(img_path)
            img_ori = np.array(copy.deepcopy(img))
            img_ori_h, img_ori_w = img_ori.shape[0], img_ori.shape[1]
            img = img.resize((640, 384))
            
            img_det = np.array(img)

            if torch.cuda.is_available():
                img = transformImg(img).unsqueeze(0).cuda()
            else:
                img = transformImg(img).unsqueeze(0).cpu()


            with torch.no_grad():
                det_out, da_seg_out, ll_seg_out = model_2d(img)
            
            inf_out, _ = det_out
            det_pred = non_max_suppression(inf_out, conf_thres=0.25, iou_thres=0.45, classes=None, agnostic=False)
            det=det_pred[0]
            # 원래 사이즈로 bbox 복원
            if det.shape[0]!=0:
                bbox_2d = np.array(det[det[:, 5]<=2].detach().cpu())*np.array([img_ori_w/640, img_ori_h/384, img_ori_w/640, img_ori_h/384, 1, 1])
            else:
                bbox_2d = np.empty((0, 6))
            _, _, height, width = img.shape
            h,w,_=img_det.shape

            _, da_seg_out = torch.max(da_seg_out, 1)
            da_seg_out = da_seg_out.int().squeeze().cpu().numpy()

            _, ll_seg_out = torch.max(ll_seg_out, 1)
            ll_seg_out = ll_seg_out.int().squeeze().cpu().numpy()
            img_det = show_seg_result(img_det, img_ori_h, img_ori_w, (da_seg_out, ll_seg_out), _, _, is_demo=True)
            if len(det):
                # det[:,:4] = scale_coords(img.shape[2:],det[:,:4],img_det.shape).round()
                det[:, :4] *= torch.tensor([img_ori_w/640, img_ori_h/384, img_ori_w/640, img_ori_h/384]).cuda()
                for *xyxy,conf,cls in reversed(det):
                    label_det_pred = f'{names[int(cls)]} {conf:.2f}'
                    plot_one_box(xyxy, img_det, label=label_det_pred, color=colors[int(cls)], line_thickness=2)
            cv2.imwrite(f'/opt/ml/images/2D_recursive_sort/image{current_frame:06d}.png', img_det[:, :, [2, 1, 0]])

            """
            3D start
            """
            # 현재 차량의 utm_x, utm_y, yaw를 구한다.

            # oxt_data = oxt_datas[i].rstrip().split(' ')
            oxt_fname = os.path.join(cfg.data_root, 'oxts', 'data', str(current_frame).zfill(6)+'.txt')
            with open(oxt_fname, 'r') as oxt_file:
                oxt_data = oxt_file.readlines() 
            oxt_data = oxt_data[0].rstrip().split(' ')
            oxt_data = np.array(oxt_data, dtype=np.float32)
            utm_coord = from_latlon(oxt_data[0], oxt_data[1])
            utm_x, utm_y = utm_coord[0], utm_coord[1] 
            oxt_dict[current_frame] = np.array([utm_x, utm_y, oxt_data[5], oxt_data[6], oxt_data[7]]) # utm_x, utm_y, yaw


            with torch.no_grad():
                result = model(return_loss=False, rescale=True, **data)
            
            """
            """
            gt, name = Cam2LidarBev_tracking(calib, label, current_frame)
            gt= np.array(gt)[:, [0, 1, 3, 2, 4]]
            gt_points = bevPoints_tracking(gt)
            """
            """
            
            # 모든 lidar BEV view
            velodyne_path = dataset[current_frame]['img_metas'][0].data['pts_filename']
            top, density_image, points_filtered = lidar2Bev(velodyne_path)
            
            # 모든 detection BEV view
            # total_det는 n x 8 numpy array -> x, y, z, l, w, h, yaw, score, cls_id
            labels = result[0]
            indices = np.where(labels['scores_3d'] >= 0.6)
            total_det = np.concatenate((labels['boxes_3d'].tensor.numpy()[indices], labels['scores_3d'].numpy()[indices][:, np.newaxis], labels['labels_3d'].numpy()[indices][:, np.newaxis]), axis=1)
            # tracking
            trackers = mot_tracker.update(total_det, bbox_2d[:, :4], points_filtered)
            # x,y,rot,h,w -> x1,y1,x2,y2,x3,y3,x4,y4
            
            
            forecast_dict, total_updated_ids, predicted_updated_ids = makeForecastDict(trackers, forecast_dict, filtered_updated_ids, prev_forecast, predicted_updated_ids, oxt_dict, current_frame)
            filtered_updated_ids, filtered_forecast_dict = filterUpdatedIds(forecast_dict, total_updated_ids)

            # print(trackers) -> x, y, rot, l, w, tracking_id, cls_id, score, updated_coordinate(x, y, z, w, l, h, yaw)
            rotated_points= bevPoints(trackers)
            rotated_points_detections = bevPoints(total_det[:, [0, 1, 6, 3, 4]])

            # draw bbox on bev lidar points
            density_image, density_image_detect = drawBbox(rotated_points, trackers, rotated_points_detections, density_image, current_frame, tracking_file)

            """
            """
            # for j, point in enumerate(gt_points):
            #     point = point.reshape(-1, 2).astype(np.int32)[:,::-1]
            #     density_image = cv2.polylines(density_image, [point], True, (0, 0, 255), thickness=1)
            #     x_max = point[:, 0].max()
            #     y_max = point[:, 1].max()
            #     cv2.putText(density_image, str(j)+name[j], (x_max, y_max), 0, 0.7, (0, 0, 255), thickness=2, lineType=cv2.LINE_AA)
            """
            """
            

            for track in trackers:
                print("%d, %d, %.3f, %.3f, %.3f, %.3f, %.3f, %.3f, %.3f"%(current_frame, track[5], track[8], track[9], track[10], track[11], track[12], track[13], track[14]), file=bbox_3d_txt)
            
            for bbox in  bbox_2d[:, :4]:
                print("%d, %d, %d, %d, %d"%(current_frame, bbox[0], bbox[1], bbox[2], bbox[3]), file=bbox_2d_txt)
            


            """
            forecast start
            """
            recovered_pfs_lidar = np.empty((0, 12, 2))
            if len(filtered_forecast_dict)!=0:
                forecast_test_dataset = SocialDataset(filtered_forecast_dict, set_name="test", b_size=25, t_tresh=0, d_tresh=25, verbose=True)

                recovery = np.empty((0, 0, 0))
                for traj in forecast_test_dataset.trajectory_batches:
                    recovery = copy.deepcopy(traj[:, :1, :])
                    traj -= traj[:, :1, :]
                    
                    traj *= (hyper_params["data_scale"]*10)
                device = 'cuda'
                prev_forecast, density_image, recovered_pfs_lidar, recovered_pfs = forecastTest(forecast_test_dataset, model_forecast, device, hyper_params, density_image, recovery, forecast_dict, filtered_updated_ids,oxt_dict,best_of_n = 1)
            # """
            # lane projection start
            # """

            # # velodyne_path : 라이다 파일 경로
            # # calib         : calibration 정보
            # # ll_seg_out    : 차선 정보

            # lane_coord = []
            # pc = copy.deepcopy(points_filtered)
            # points_projection = project_lidar2img(img_ori, points_filtered, v2p_matrix)
            # pc = pc[(points_projection[:,0]<1242) & (1<points_projection[:,0]) & (1<points_projection[:,1]) & (points_projection[:,1]<375)]
            # points_projection = points_projection[(points_projection[:,0]<1242) & (1<points_projection[:,0]) & (1<points_projection[:,1]) & (points_projection[:,1]<375)]
        
            # # ll_seg_out : (384, 640), min=0, max=1
            # LANE_IMG = np.array(ll_seg_out, dtype=np.float32)
            # LANE_IMG = cv2.resize(LANE_IMG, (1242, 375))

            # for idx,i in enumerate(points_projection):
            #     if LANE_IMG[int(i[1]),int(i[0])]:
            #         lane_coord.append(idx)
            
            # #density_image.shape : (704, 800)
            # lane_xy = []
            # for i in lane_coord:
            #     lane_xy.append(list(pc[i,:2]))
            # lane_xy.sort()
            # lane_xy = np.array(lane_xy)

            # global lane_0
            # lane_0 = lane_xy[0:1]
            # def lane_classification(idx, lane_number, max_lane):
            #     global lane_t_num
            #     if abs(lane_xy[idx,1] - lane_xy[idx+1,1]) <= 1.5:
            #         if len(lane_xy) > idx+2:
            #             globals()[f'lane_{lane_number}'] = np.vstack((globals()[f'lane_{lane_number}'], lane_xy[idx+1:idx+2]))
            #             lane_classification(idx+1, lane_number, max_lane)

            #     elif len(lane_xy) > idx+2:
                    
            #         s = 1
            #         for i in range(max_lane+1):

            #             if abs(globals()[f'lane_{i}'][-1, 1] - lane_xy[idx+1, 1]) <= 1.5:

            #                 globals()[f'lane_{i}'] = np.vstack((globals()[f'lane_{i}'], lane_xy[idx+1:idx+2]))
            #                 s = 0
            #                 lane_number = i
            #                 break
            #         if s:
            #             max_lane += 1
            #             globals()[f'lane_{max_lane}'] = lane_xy[idx+1:idx+2]
            #             lane_number = max_lane
            #             lane_t_num = max_lane + 1
            #         lane_classification(idx+1, lane_number, max_lane)

            
            

            # lane_classification(0, 0, 0)
            # LANE = []
            # LANE_DICT = {}
            
            # for i in range(lane_t_num):
            #     if len(globals()[f'lane_{i}']) >= len(lane_xy)//20:
            #         LANE.append(globals()[f'lane_{i}'])
            # for i in LANE:
            #     LANE_DICT[abs(np.mean(i[:,1]))] = i
            # LANE_OURS = [LANE_DICT.pop(min(LANE_DICT)), LANE_DICT.pop(min(LANE_DICT))]
            
        

            # for lane_xy in LANE_OURS:
            #     X = lane_xy[:,0]
            #     y = lane_xy[:,1]
            #     X = X.reshape(-1, 1)

            #     # Fit line using all data
            #     lr = linear_model.LinearRegression()
            #     lr.fit(add_square_feature(X), y)

            #     # Robustly fit linear model with RANSAC algorithm
            #     ransac = linear_model.RANSACRegressor()
            #     ransac.fit(add_square_feature(X), y)
                

            #     # Predict data of estimated models
            #     line_X = np.arange(X.min(), X.max())[:, np.newaxis]
            #     line_y_ransac = ransac.predict(add_square_feature(line_X))

            #     # print(line_X) # (N, 1)
            #     # print(line_y_ransac) # (N, )
            #     line_coord = np.concatenate((line_X, line_y_ransac[:, np.newaxis]), axis=1)
            #     line_coord_bev = center2ImageBev(line_coord).astype(np.int32)
                
            #     for i in range(line_coord_bev.shape[0]):
            #         if i != 0:
            #             density_image = cv2.line(density_image, (line_coord_bev[i-1][1], line_coord_bev[i-1][0]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (255, 0, 0), thickness=2)
            #         else:
            #             density_image = cv2.line(density_image, (line_coord_bev[i][1], density_image.shape[1]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (255, 0, 0), thickness=2)

            
            
            check_dist = 30
            lane_coord = []
            pc = copy.deepcopy(points_filtered)
            points_projection = project_lidar2img(img_ori, points_filtered, v2p_matrix)
            pc = pc[(points_projection[:,0]<img_ori_w) & (1<points_projection[:,0]) & (1<points_projection[:,1]) & (points_projection[:,1]<img_ori_h)]
            points_projection = points_projection[(points_projection[:,0]<img_ori_w) & (1<points_projection[:,0]) & (1<points_projection[:,1]) & (points_projection[:,1]<img_ori_h)]

            # ll_seg_out : (384, 640), min=0, max=1
            LANE_IMG = np.array(ll_seg_out, dtype=np.float32)
            LANE_IMG[:LANE_IMG.shape[0]//2,:] = 0
            for i in range(LANE_IMG.shape[0]//2, LANE_IMG.shape[0]):
                LANE_IMG[i, :(LANE_IMG.shape[1]//4 + int((LANE_IMG.shape[0]-i)*(LANE_IMG.shape[1]//8)/(LANE_IMG.shape[0]//2)))] = 0
                LANE_IMG[i, (LANE_IMG.shape[1]//4*3 - int((LANE_IMG.shape[0]-i)*(LANE_IMG.shape[1]//8)/(LANE_IMG.shape[0]//2))):] = 0
            LANE_IMG = cv2.resize(LANE_IMG, (img_ori_w, img_ori_h))

            for idx,i in enumerate(points_projection):
                if LANE_IMG[int(i[1]),int(i[0])]:
                    lane_coord.append(idx)

            #density_image.shape : (704, 800)
            lane_xy = []
            for i in lane_coord:
                lane_xy.append(list(pc[i,:2]))
            lane_xy.sort()
            lane_xy = np.array(lane_xy)

            global lane_0
            lane_0 = lane_xy[0:1]
            
            lane_t_num = 0
            def lane_classification(idx, lane_number, max_lane):
                global lane_t_num
                if abs(lane_xy[idx,1] - lane_xy[idx+1,1]) <= 0.3:
                    if len(lane_xy) > idx+2:
                        globals()[f'lane_{lane_number}'] = np.vstack((globals()[f'lane_{lane_number}'], lane_xy[idx+1:idx+2]))
                        lane_classification(idx+1, lane_number, max_lane)

                elif len(lane_xy) > idx+2:
                    s = 1
                    for i in range(max_lane+1):

                        if abs(globals()[f'lane_{i}'][-1, 1] - lane_xy[idx+1, 1]) <= 0.3:

                            globals()[f'lane_{i}'] = np.vstack((globals()[f'lane_{i}'], lane_xy[idx+1:idx+2]))
                            s = 0
                            lane_number = i
                            break
                    if s:
                        max_lane += 1
                        globals()[f'lane_{max_lane}'] = lane_xy[idx+1:idx+2]
                        lane_number = max_lane
                        lane_t_num = max_lane + 1
                    lane_classification(idx+1, lane_number, max_lane)
                
            if len(lane_xy) > 10:
                lane_classification(0, 0, 0)
                LANE = []
                LANE_DICT = {}

                for i in range(lane_t_num):
                    if len(globals()[f'lane_{i}']) >= len(lane_xy)//10:
                        LANE.append(globals()[f'lane_{i}'])
                for i in LANE:
                    LANE_DICT[abs(np.mean(i[:,1]))] = i
                if len(LANE_DICT) < 2:
                    LANE_OURS = pre_LANE_OURS
                else:
                    LANE_OURS = [LANE_DICT.pop(min(LANE_DICT)), LANE_DICT.pop(min(LANE_DICT))]
                    if 1 < np.mean(LANE_OURS[0][:5,1]) < 2:
                        LANE_LEFT = LANE_OURS[0]
                        if -2 < np.mean(LANE_OURS[1][:5,1]) < -1:
                            LANE_RIGHT = LANE_OURS[1]
                        else:
                            if len(pre_LANE_RIGHT) <= 3:
                                LANE_RIGHT = LANE_LEFT*np.array([1, -1])
                            else:
                                LANE_RIGHT = pre_LANE_RIGHT

                    elif -2 < np.mean(LANE_OURS[0][:5,1]) < -1:
                        LANE_RIGHT = LANE_OURS[0]
                        if 1 < np.mean(LANE_OURS[1][:5,1]) < 2:
                            LANE_LEFT = LANE_OURS[1]
                        else:
                            if len(pre_LANE_LEFT) <= 3:
                                LANE_LEFT = LANE_RIGHT*np.array([1, -1])
                            else:
                                LANE_LEFT = pre_LANE_LEFT
                    else:
                        if 1 < np.mean(LANE_OURS[1][:5,1]) < 2:
                            LANE_LEFT = LANE_OURS[1]
                            if len(pre_LANE_RIGHT) <= 3:
                                LANE_RIGHT = LANE_LEFT*np.array([1, -1])
                            else:
                                LANE_RIGHT = pre_LANE_RIGHT

                        elif -2 < np.mean(LANE_OURS[1][:5,1]) < -1:
                            LANE_RIGHT = LANE_OURS[1]
                            if len(pre_LANE_LEFT) <=3:
                                LANE_LEFT = LANE_RIGHT*np.array([1, -1])
                            else:
                                LANE_LEFT = pre_LANE_LEFT
                        else:
                            LANE_RIGHT = pre_LANE_RIGHT
                            LANE_LEFT = pre_LANE_LEFT

                    
                    pre_LANE_LEFT = LANE_LEFT
                    pre_LANE_RIGHT = LANE_RIGHT
                    LANE_OURS = [LANE_LEFT, LANE_RIGHT]                

                

                if len(LANE_OURS[0]) > 4:
                    X = LANE_OURS[0][:,0]
                    y = LANE_OURS[0][:,1]
                    X = X.reshape(-1, 1)

                    # Fit line using all data
                    lr = linear_model.LinearRegression()
                    lr.fit(add_square_feature(X), y)

                    # Robustly fit linear model with RANSAC algorithm
                    ransac = linear_model.RANSACRegressor()
                    ransac.fit(add_square_feature(X), y)
                    

                    # Predict data of estimated models
                    line_X = np.arange(1, 30)[:, np.newaxis]
                    line_y_ransac = ransac.predict(add_square_feature(line_X))

                    # print(line_X) # (N, 1)
                    # print(line_y_ransac) # (N, )
                    line_coord = np.concatenate((line_X, line_y_ransac[:, np.newaxis]), axis=1)
                    line_coord_bev = center2ImageBev(line_coord).astype(np.int32)
                    
                    if abs(line_y_ransac[28] - line_y_ransac[1]) < 0.5:
                        for i in range(line_coord_bev.shape[0]):
                            if i != 0:
                                density_image = cv2.line(density_image, (line_coord_bev[i-1][1], line_coord_bev[i-1][0]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                            else:
                                density_image = cv2.line(density_image, (line_coord_bev[i][1], density_image.shape[1]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                        pre_line_X_left = line_X
                        pre_line_Y_left = line_y_ransac
                    else:
                        if type(pre_line_Y_left) == np.ndarray:
                            line_coord = np.concatenate((line_X, pre_line_Y_left[:, np.newaxis]), axis=1)
                            line_coord_bev = center2ImageBev(line_coord).astype(np.int32)
                            for i in range(line_coord_bev.shape[0]):
                                if i != 0:
                                    density_image = cv2.line(density_image, (line_coord_bev[i-1][1], line_coord_bev[i-1][0]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                                else:
                                    density_image = cv2.line(density_image, (line_coord_bev[i][1], density_image.shape[1]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                    
                    if type(pre_line_Y_left) == np.ndarray:
                        left_a = (pre_line_Y_left[-1]-pre_line_Y_left[0])/(check_dist-1)
                        left_b = pre_line_Y_left[0]-left_a

                else:
                    if type(pre_line_Y_left) == np.ndarray:
                        line_coord = np.concatenate((line_X, pre_line_Y_left[:, np.newaxis]), axis=1)
                        line_coord_bev = center2ImageBev(line_coord).astype(np.int32)
                        for i in range(line_coord_bev.shape[0]):
                            if i != 0:
                                density_image = cv2.line(density_image, (line_coord_bev[i-1][1], line_coord_bev[i-1][0]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                            else:
                                density_image = cv2.line(density_image, (line_coord_bev[i][1], density_image.shape[1]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                    if type(pre_line_Y_left) == np.ndarray:
                        left_a = (pre_line_Y_left[-1]-pre_line_Y_left[0])/(check_dist-1)
                        left_b = pre_line_Y_left[0]-left_a
                
                if len(LANE_OURS[1]) > 4:
                    X = LANE_OURS[1][:,0]
                    y = LANE_OURS[1][:,1]
                    X = X.reshape(-1, 1)

                    # Fit line using all data
                    lr = linear_model.LinearRegression()
                    lr.fit(add_square_feature(X), y)

                    # Robustly fit linear model with RANSAC algorithm
                    ransac = linear_model.RANSACRegressor()
                    ransac.fit(add_square_feature(X), y)
                    

                    # Predict data of estimated models
                    line_X = np.arange(1, 30)[:, np.newaxis]
                    line_y_ransac = ransac.predict(add_square_feature(line_X))

                    # print(line_X) # (N, 1)
                    # print(line_y_ransac) # (N, )
                    line_coord = np.concatenate((line_X, line_y_ransac[:, np.newaxis]), axis=1)
                    line_coord_bev = center2ImageBev(line_coord).astype(np.int32)
                    
                    if abs(line_y_ransac[28] - line_y_ransac[1]) < 0.5:
                        for i in range(line_coord_bev.shape[0]):
                            if i != 0:
                                density_image = cv2.line(density_image, (line_coord_bev[i-1][1], line_coord_bev[i-1][0]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                            else:
                                density_image = cv2.line(density_image, (line_coord_bev[i][1], density_image.shape[1]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                        pre_line_X_right = line_X
                        pre_line_Y_right = line_y_ransac
                    
                    else:
                        if type(pre_line_Y_right) == np.ndarray:
                            line_coord = np.concatenate((line_X, pre_line_Y_right[:, np.newaxis]), axis=1)
                            line_coord_bev = center2ImageBev(line_coord).astype(np.int32)
                            for i in range(line_coord_bev.shape[0]):
                                if i != 0:
                                    density_image = cv2.line(density_image, (line_coord_bev[i-1][1], line_coord_bev[i-1][0]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                                else:
                                    density_image = cv2.line(density_image, (line_coord_bev[i][1], density_image.shape[1]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                    if type(pre_line_Y_right) == np.ndarray:
                        right_a = (pre_line_Y_right[-1]-pre_line_Y_right[0])/(check_dist-1)
                        right_b = pre_line_Y_right[0]-right_a
                else:
                    if type(pre_line_Y_right) == np.ndarray:
                        line_coord = np.concatenate((line_X, pre_line_Y_right[:, np.newaxis]), axis=1)
                        line_coord_bev = center2ImageBev(line_coord).astype(np.int32)
                        for i in range(line_coord_bev.shape[0]):
                            if i != 0:
                                density_image = cv2.line(density_image, (line_coord_bev[i-1][1], line_coord_bev[i-1][0]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                            else:
                                density_image = cv2.line(density_image, (line_coord_bev[i][1], density_image.shape[1]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                    if type(pre_line_Y_right) == np.ndarray:    
                        right_a = (pre_line_Y_right[-1]-pre_line_Y_right[0])/(check_dist-1)
                        right_b = pre_line_Y_right[0]-right_a
            
            else:
                line_X = np.arange(1, 30)[:, np.newaxis]
                line_y_ransac = pre_line_Y_left
                # print(line_X) # (N, 1)
                # print(line_y_ransac) # (N, )
                line_coord = np.concatenate((line_X, line_y_ransac[:, np.newaxis]), axis=1)
                line_coord_bev = center2ImageBev(line_coord).astype(np.int32)
                for i in range(line_coord_bev.shape[0]):
                    if i != 0:
                        density_image = cv2.line(density_image, (line_coord_bev[i-1][1], line_coord_bev[i-1][0]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                    else:
                        density_image = cv2.line(density_image, (line_coord_bev[i][1], density_image.shape[1]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)

                line_X = np.arange(1, 30)[:, np.newaxis]
                line_y_ransac = pre_line_Y_right
                # print(line_X) # (N, 1)
                # print(line_y_ransac) # (N, )
                line_coord = np.concatenate((line_X, line_y_ransac[:, np.newaxis]), axis=1)
                line_coord_bev = center2ImageBev(line_coord).astype(np.int32)
                for i in range(line_coord_bev.shape[0]):
                    if i != 0:
                        density_image = cv2.line(density_image, (line_coord_bev[i-1][1], line_coord_bev[i-1][0]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)
                    else:
                        density_image = cv2.line(density_image, (line_coord_bev[i][1], density_image.shape[1]), (line_coord_bev[i][1], line_coord_bev[i][0]), color = (0, 0, 255), thickness=2)

            print(">>>>>>>>>>")
            print(type(pre_line_Y_left))
            print(type(pre_line_Y_right))

            if type(pre_line_Y_left) != np.ndarray:
                if type(pre_line_Y_right) == np.ndarray:
                    pre_line_Y_left = pre_line_Y_right * -1
                
            if type(pre_line_Y_right) != np.ndarray:
                if type(pre_line_Y_left) == np.ndarray:
                    pre_line_Y_right = pre_line_Y_left * -1

            warning_mask = np.zeros_like(density_image)
            if type(pre_line_Y_left) == np.ndarray and type(pre_line_Y_right) == np.ndarray:
                if recovered_pfs_lidar.shape[0]!=0:
                    for N, points_lidar_ in enumerate(recovered_pfs_lidar):
                        # points_lidar : (12, 2)
                        for P, point_lidar_ in enumerate(points_lidar_):
                            if( checkWarning(point_lidar_[0], point_lidar_[1], left_a, left_b, right_a, right_b, check_dist) ):
                                cv2.circle(warning_mask, (int(recovered_pfs[N][P][1]), int(recovered_pfs[N][P][0])), 20, (255, 0, 0), -1)
                                break

                

            density_image = cv2.addWeighted(density_image, 1.0, warning_mask, 0.7, 0.0)            

            cv2.imwrite(f'/opt/ml/images/3D_recursive_sort/image{current_frame:06d}.png', density_image[:, :, [2, 1, 0]])
            cv2.imwrite(f'/opt/ml/images/3D_recursive_sort_detect/image{current_frame:06d}.png', density_image_detect)
            results.extend(result)
            bev_results.extend(total_det)
            batch_size = len(result)
            for _ in range(batch_size):
                prog_bar.update()
        bbox_2d_txt.close()
        bbox_3d_txt.close()   
    return results, bev_results


    
