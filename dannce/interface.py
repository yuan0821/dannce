import sys
import numpy as np
import os
from copy import deepcopy
import scipy.io as sio
import imageio
import time

import tensorflow as tf
import tensorflow.keras as keras
import tensorflow.keras.losses as keras_losses
from tensorflow.keras.models import load_model, Model
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.callbacks import ModelCheckpoint, CSVLogger, TensorBoard
import tensorflow.keras.backend as K
from tensorflow.random import set_seed

import dannce.engine.serve_data_DANNCE as serve_data_DANNCE
import dannce.engine.serve_data_COM as serve_data_COM

from dannce.engine.generator import DataGenerator_3Dconv
from dannce.engine.generator import DataGenerator_3Dconv_frommem
from dannce.engine.generator import DataGenerator_3Dconv_torch
from dannce.engine.generator import DataGenerator_3Dconv_tf
from dannce.engine.generator_aux import DataGenerator_downsample
import dannce.engine.processing as processing
from dannce.engine.processing import savedata_tomat, savedata_expval
from dannce.engine import nets
from dannce.engine import losses
from dannce.engine import ops
from six.moves import cPickle

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def com_predict(base_config_path):
    # Load in the params
    base_params = processing.read_config(base_config_path)
    base_params = processing.make_paths_safe(base_params)

    params = processing.read_config(base_params["io_config"])
    params = processing.make_paths_safe(params)
    params = processing.inherit_config(params, base_params, list(base_params.keys()))

    # Load the appropriate loss function and network
    try:
        params["loss"] = getattr(losses, params["loss"])
    except AttributeError:
        params["loss"] = getattr(keras_losses, params["loss"])
    params["net"] = getattr(nets, params["net"])

    vid_dir_flag = params["vid_dir_flag"]
    _N_VIDEO_FRAMES = params["chunks"]

    os.environ["CUDA_VISIBLE_DEVICES"] = params["gpuID"]

    # If params['N_CHANNELS_OUT'] is greater than one, we enter a mode in
    # which we predict all available labels + the COM
    MULTI_MODE = params["N_CHANNELS_OUT"] > 1
    params["N_CHANNELS_OUT"] = params["N_CHANNELS_OUT"] + int(MULTI_MODE)

    # Grab the input file for prediction
    params["label3d_file"] = processing.grab_predict_label3d_file()

    # Also add parent params under the 'experiment' key for compatibility
    # with DANNCE's video loading function
    params["experiment"] = params

    # Build net
    print("Initializing Network...")
    model = params["net"](
        params["loss"],
        float(params["lr"]),
        params["N_CHANNELS_IN"],
        params["N_CHANNELS_OUT"],
        params["metric"],
        multigpu=False,
    )

    if "predict_weights" in params.keys():
        model.load_weights(params["predict_weights"])
    else:
        wdir = params["com_train_dir"]  # os.path.join('.', 'COM', 'train_results')
        weights = os.listdir(wdir)
        weights = [f for f in weights if ".hdf5" in f]
        weights = sorted(weights, key=lambda x: int(x.split(".")[1].split("-")[0]))
        weights = weights[-1]

        print("Loading weights from " + os.path.join(wdir, weights))
        model.load_weights(os.path.join(wdir, weights))

    print("COMPLETE\n")

    def evaluate_ondemand(start_ind, end_ind, valid_gen):
        """Perform COM detection over a set of frames.

        :param start_ind: Starting frame index
        :param end_ind: Ending frame index
        :param steps: Subsample every steps frames
        """
        end_time = time.time()
        sample_save = 100
        for i in range(start_ind, end_ind):
            print("Predicting on sample {}".format(i), flush=True)
            if (i - start_ind) % sample_save == 0 and i != start_ind:
                print(i)
                print(
                    "{} samples took {} seconds".format(
                        sample_save, time.time() - end_time
                    )
                )
                end_time = time.time()

            if (i - start_ind) % 1000 == 0 and i != start_ind:
                print("Saving checkpoint at {}th sample".format(i))
                processing.save_COM_checkpoint(
                    save_data, com_predict_dir, datadict_, cameras, params
                )

            pred_ = model.predict(valid_gen.__getitem__(i)[0])

            pred_ = np.reshape(
                pred_,
                [
                    -1,
                    len(params["CAMNAMES"]),
                    pred_.shape[1],
                    pred_.shape[2],
                    pred_.shape[3],
                ],
            )

            for m in range(pred_.shape[0]):
                # odd loop condition, but it's because at the end of samples,
                # predict_generator will continue to make predictions in a way I
                # don't grasp yet, but also in a way we should ignore

                # By selecting -1 for the last axis, we get the COM index for a
                # normal COM network, and also the COM index for a multi_mode COM network,
                # as in multimode the COM label is put at the end
                pred = pred_[m, :, :, :, -1]
                sampleID_ = partition["valid"][i * pred_.shape[0] + m]
                save_data[sampleID_] = {}
                save_data[sampleID_]["triangulation"] = {}

                for j in range(pred.shape[0]):  # this loops over all cameras
                    # get coords for each map. This assumes that image are coming
                    # out in pred in the same order as CONFIG_PARAMS['CAMNAMES']
                    pred_max = np.max(np.squeeze(pred[j]))
                    ind = (
                        np.array(processing.get_peak_inds(np.squeeze(pred[j])))
                        * params["DOWNFAC"]
                    )
                    ind[0] += params["CROP_HEIGHT"][0]
                    ind[1] += params["CROP_WIDTH"][0]
                    ind = ind[::-1]
                    # now, the center of mass is (x,y) instead of (i,j)
                    # now, we need to use camera calibration to triangulate
                    # from 2D to 3D

                    if "COMdebug" in params.keys() and j == cnum:
                        # Write preds
                        plt.figure(0)
                        plt.cla()
                        plt.imshow(np.squeeze(pred[j]))
                        plt.savefig(
                            os.path.join(
                                cmapdir, params["COMdebug"] + str(i + m) + ".png"
                            )
                        )

                        plt.figure(1)
                        plt.cla()
                        im = valid_gen.__getitem__(i * pred_.shape[0] + m)
                        plt.imshow(processing.norm_im(im[0][j]))
                        plt.plot(
                            (ind[0] - params["CROP_WIDTH"][0]) / params["DOWNFAC"],
                            (ind[1] - params["CROP_HEIGHT"][0]) / params["DOWNFAC"],
                            "or",
                        )
                        plt.savefig(
                            os.path.join(
                                overlaydir, params["COMdebug"] + str(i + m) + ".png"
                            )
                        )

                    save_data[sampleID_][params["CAMNAMES"][j]] = {
                        "pred_max": pred_max,
                        "COM": ind,
                    }

                    # Undistort this COM here.
                    pts1 = save_data[sampleID_][params["CAMNAMES"][j]]["COM"]
                    pts1 = pts1[np.newaxis, :]
                    pts1 = ops.unDistortPoints(
                        pts1,
                        cameras[params["CAMNAMES"][j]]["K"],
                        cameras[params["CAMNAMES"][j]]["RDistort"],
                        cameras[params["CAMNAMES"][j]]["TDistort"],
                        cameras[params["CAMNAMES"][j]]["R"],
                        cameras[params["CAMNAMES"][j]]["t"],
                    )
                    save_data[sampleID_][params["CAMNAMES"][j]]["COM"] = np.squeeze(
                        pts1
                    )

                # Triangulate for all unique pairs
                for j in range(pred.shape[0]):
                    for k in range(j + 1, pred.shape[0]):
                        pts1 = save_data[sampleID_][params["CAMNAMES"][j]]["COM"]
                        pts2 = save_data[sampleID_][params["CAMNAMES"][k]]["COM"]
                        pts1 = pts1[np.newaxis, :]
                        pts2 = pts2[np.newaxis, :]

                        test3d = ops.triangulate(
                            pts1,
                            pts2,
                            camera_mats[params["CAMNAMES"][j]],
                            camera_mats[params["CAMNAMES"][k]],
                        ).squeeze()

                        save_data[sampleID_]["triangulation"][
                            "{}_{}".format(params["CAMNAMES"][j], params["CAMNAMES"][k])
                        ] = test3d

    com_predict_dir = os.path.join(params["com_predict_dir"])
    print(com_predict_dir)

    if not os.path.exists(com_predict_dir):
        os.makedirs(com_predict_dir)

    if "COMdebug" in params.keys():
        cmapdir = os.path.join(com_predict_dir, "cmap")
        overlaydir = os.path.join(com_predict_dir, "overlay")
        if not os.path.exists(cmapdir):
            os.makedirs(cmapdir)
        if not os.path.exists(overlaydir):
            os.makedirs(overlaydir)
        cnum = params["CAMNAMES"].index(params["COMdebug"])
        print("Writing " + params["COMdebug"] + " confidence maps to " + cmapdir)
        print("Writing " + params["COMdebug"] + "COM-image overlays to " + overlaydir)

    samples, datadict, datadict_3d, cameras, camera_mats = serve_data_COM.prepare_data(
        params, multimode=MULTI_MODE, prediction=True
    )

    # Zero any negative frames
    for key in datadict.keys():
        for key_ in datadict[key]["frames"].keys():
            if datadict[key]["frames"][key_] < 0:
                datadict[key]["frames"][key_] = 0

    # The generator expects an experimentID in front of each sample key
    samples = ["0_" + str(f) for f in samples]
    datadict_ = {}
    for key in datadict.keys():
        datadict_["0_" + str(key)] = datadict[key]

    datadict = datadict_

    # Initialize video dictionary. paths to videos only.
    vids = processing.initialize_vids(params, datadict, pathonly=True)

    # Parameters
    valid_params = {
        "dim_in": (
            params["CROP_HEIGHT"][1] - params["CROP_HEIGHT"][0],
            params["CROP_WIDTH"][1] - params["CROP_WIDTH"][0],
        ),
        "n_channels_in": params["N_CHANNELS_IN"],
        "batch_size": 1,
        "n_channels_out": params["N_CHANNELS_OUT"],
        "out_scale": params["SIGMA"],
        "camnames": {0: params["CAMNAMES"]},
        "crop_width": params["CROP_WIDTH"],
        "crop_height": params["CROP_HEIGHT"],
        "downsample": params["DOWNFAC"],
        "labelmode": "coord",
        "chunks": params["chunks"],
        "shuffle": False,
        "dsmode": params["dsmode"] if "dsmode" in params.keys() else "dsm",
        "preload": False,
    }

    partition = {}
    partition["valid"] = samples
    labels = datadict

    save_data = {}

    valid_generator = DataGenerator_downsample(
        partition["valid"], labels, vids, **valid_params
    )

    # If we just want to analyze a chunk of video...
    st_ind = (
        params["start_sample_index"] if "start_sample_index" in params.keys() else 0
    )
    if params["max_num_samples"] == "max":
        evaluate_ondemand(st_ind, len(valid_generator), valid_generator)
    else:
        endIdx = np.min([st_ind + params["max_num_samples"], len(valid_generator)])
        evaluate_ondemand(st_ind, endIdx, valid_generator)

    processing.save_COM_checkpoint(
        save_data, com_predict_dir, datadict_, cameras, params
    )

    print("done!")


def com_train(base_config_path):
    # Set up parameters
    base_params = processing.read_config(base_config_path)
    base_params = processing.make_paths_safe(base_params)

    params = processing.read_config(base_params["io_config"])
    params = processing.make_paths_safe(params)
    params = processing.inherit_config(params, base_params, list(base_params.keys()))

    params["loss"] = getattr(losses, params["loss"])
    params["net"] = getattr(nets, params["net"])

    os.environ["CUDA_VISIBLE_DEVICES"] = params["gpuID"]

    samples = []
    datadict = {}
    datadict_3d = {}
    cameras = {}
    camnames = {}

    label3d_files = params["com_label3d_files"]
    print(label3d_files, flush=True)
    num_experiments = len(label3d_files)
    params["experiment"] = {}
    MULTI_MODE = params["N_CHANNELS_OUT"] > 1
    params["N_CHANNELS_OUT"] = params["N_CHANNELS_OUT"] + int(MULTI_MODE)
    for e, label3d_file in enumerate(label3d_files):
        exp = params.copy()
        exp = processing.make_paths_safe(exp)
        exp["label3d_file"] = label3d_file
        print(exp["label3d_file"])
        exp["base_exp_folder"] = os.path.dirname(exp["label3d_file"])
        exp["viddir"] = os.path.join(exp["base_exp_folder"], exp["viddir"])

        params["experiment"][e] = exp
        (
            samples_,
            datadict_,
            datadict_3d_,
            data_3d_,
            cameras_,
        ) = serve_data_DANNCE.prepare_data(
            params["experiment"][e],
            nanflag=False,
            com_flag=not MULTI_MODE,
            multimode=MULTI_MODE,
        )

        # No need to prepare any COM file (they don't exist yet).
        # We call this because we want to support multiple experiments,
        # which requires appending the experiment ID to each data object and key
        samples, datadict, datadict_3d, ddd = serve_data_DANNCE.add_experiment(
            e,
            samples,
            datadict,
            datadict_3d,
            {},
            samples_,
            datadict_,
            datadict_3d_,
            {},
        )
        cameras[e] = cameras_
        camnames[e] = params["experiment"][e]["CAMNAMES"]

    com_train_dir = params["com_train_dir"]
    print(com_train_dir)

    if not os.path.exists(com_train_dir):
        os.makedirs(com_train_dir)

    # Additionally, to keep videos unique across experiments, need to add
    # experiment labels in other places. E.g. experiment 0 CameraE's "camname"
    # Becomes 0_CameraE.
    cameras, datadict, params = serve_data_DANNCE.prepend_experiment(
        params, datadict, num_experiments, camnames, cameras
    )

    samples = np.array(samples)

    e = 0

    # Initialize video objects
    vids = {}
    for e in range(num_experiments):
        vids = processing.initialize_vids_train(
            params, datadict, e, vids, pathonly=True
        )

    print(
        "Using {} downsampling".format(
            params["dsmode"] if "dsmode" in params.keys() else "dsm"
        )
    )

    train_params = {
        "dim_in": (
            params["CROP_HEIGHT"][1] - params["CROP_HEIGHT"][0],
            params["CROP_WIDTH"][1] - params["CROP_WIDTH"][0],
        ),
        "n_channels_in": params["N_CHANNELS_IN"],
        "batch_size": 1,
        "n_channels_out": params["N_CHANNELS_OUT"],
        "out_scale": params["SIGMA"],
        "camnames": camnames,
        "crop_width": params["CROP_WIDTH"],
        "crop_height": params["CROP_HEIGHT"],
        "downsample": params["DOWNFAC"],
        "shuffle": False,
        "chunks": params["chunks"],
        "dsmode": params["dsmode"] if "dsmode" in params.keys() else "dsm",
        "preload": False,
    }

    valid_params = deepcopy(train_params)
    valid_params["shuffle"] = False

    partition = {}
    if "load_valid" not in params.keys():

        all_inds = np.arange(len(samples))

        # extract random inds from each set for validation
        v = params["num_validation_per_exp"]
        valid_inds = []
        for e in range(num_experiments):
            tinds = [
                i for i in range(len(samples)) if int(samples[i].split("_")[0]) == e
            ]
            valid_inds = valid_inds + list(np.random.choice(tinds, (v,), replace=False))
            valid_inds = list(np.sort(valid_inds))

        train_inds = [i for i in all_inds if i not in valid_inds]
        assert (set(valid_inds) & set(train_inds)) == set()

        partition["train"] = samples[train_inds]
        partition["valid"] = samples[valid_inds]
    else:
        # Load validation samples from elsewhere
        with open(os.path.join(params["load_valid"], "val_samples.pickle"), "rb") as f:
            partition["valid"] = cPickle.load(f)
        partition["train"] = [f for f in samples if f not in partition["valid"]]

    # Optionally, we can subselect a number of random train indices
    if "num_train_per_exp" in params.keys():
        nt = params["num_train_per_exp"]
        subtrain = []
        for e in range(num_experiments):
            tinds = np.array(
                [i for i in partition["train"] if int(i.split("_")[0]) == e]
            )
            tinds_ = np.random.choice(np.arange(len(tinds)), (nt,), replace=False)
            tinds_ = np.sort(tinds_)
            subtrain = subtrain + list(tinds[tinds_])

        partition["train"] = subtrain

    # Save train/val inds
    with open(com_train_dir + "val_samples.pickle", "wb") as f:
        cPickle.dump(partition["valid"], f)

    with open(com_train_dir + "train_samples.pickle", "wb") as f:
        cPickle.dump(partition["train"], f)

    labels = datadict

    # Build net
    print("Initializing Network...")

    # with tf.device("/gpu:0"):
    model = params["net"](
        params["loss"],
        float(params["lr"]),
        params["N_CHANNELS_IN"],
        params["N_CHANNELS_OUT"],
        params["metric"],
        multigpu=False,
    )
    print("COMPLETE\n")

    if params["weights"] is not None:
        weights = os.listdir(params["weights"])
        weights = [f for f in weights if ".hdf5" in f]
        weights = weights[0]

        try:
            model.load_weights(os.path.join(params["weights"], weights))
        except:
            print(
                "Note: model weights could not be loaded due to a mismatch in dimensions.\
                   Assuming that this is a fine-tune with a different number of outputs and removing \
                  the top of the net accordingly"
            )
            model.layers[-1].name = "top_conv"
            model.load_weights(os.path.join(params["weights"], weights), by_name=True)

    if "lockfirst" in params.keys() and params["lockfirst"]:
        for layer in model.layers[:2]:
            layer.trainable = False

    model.compile(
        optimizer=Adam(lr=float(params["lr"])), loss=params["loss"], metrics=["mse"],
    )

    # Create checkpoint and logging callbacks
    model_checkpoint = ModelCheckpoint(
        os.path.join(com_train_dir, "weights.{epoch:02d}-{val_loss:.5f}.hdf5"),
        monitor="loss",
        save_best_only=True,
        save_weights_only=True,
    )
    csvlog = CSVLogger(os.path.join(com_train_dir, "training.csv"))
    tboard = TensorBoard(
        log_dir=com_train_dir + "logs", write_graph=False, update_freq=100
    )

    # Initialize data structures
    ncams = len(camnames[0])
    dh = (params["CROP_HEIGHT"][1] - params["CROP_HEIGHT"][0]) // params["DOWNFAC"]
    dw = (params["CROP_WIDTH"][1] - params["CROP_WIDTH"][0]) // params["DOWNFAC"]
    ims_train = np.zeros((ncams * len(partition["train"]), dh, dw, 3), dtype="float32")
    y_train = np.zeros(
        (ncams * len(partition["train"]), dh, dw, params["N_CHANNELS_OUT"]),
        dtype="float32",
    )
    ims_valid = np.zeros((ncams * len(partition["valid"]), dh, dw, 3), dtype="float32")
    y_valid = np.zeros(
        (ncams * len(partition["valid"]), dh, dw, params["N_CHANNELS_OUT"]),
        dtype="float32",
    )

    # When there are a lot of videos
    train_generator = DataGenerator_downsample(
        partition["train"], labels, vids, **train_params
    )
    valid_generator = DataGenerator_downsample(
        partition["valid"], labels, vids, **valid_params
    )

    print("Loading data")
    for i in range(len(partition["train"])):
        print(i, end="\r")
        ims = train_generator.__getitem__(i)
        ims_train[i * ncams : (i + 1) * ncams] = ims[0]
        y_train[i * ncams : (i + 1) * ncams] = ims[1]

    for i in range(len(partition["valid"])):
        ims = valid_generator.__getitem__(i)
        ims_valid[i * ncams : (i + 1) * ncams] = ims[0]
        y_valid[i * ncams : (i + 1) * ncams] = ims[1]

    if params["debug"] and not MULTI_MODE:
        # Plot all training images and save
        # create new directory for images if necessary
        debugdir = os.path.join(params["com_train_dir"], "debug_im_out")
        print("Saving debug images to: " + debugdir)
        if not os.path.exists(debugdir):
            os.makedirs(debugdir)

        plt.figure()
        for i in range(ims_train.shape[0]):
            plt.cla()
            processing.plot_markers_2d(
                processing.norm_im(ims_train[i]), y_train[i], newfig=False
            )
            plt.gca().xaxis.set_major_locator(plt.NullLocator())
            plt.gca().yaxis.set_major_locator(plt.NullLocator())

            imname = str(i) + ".png"
            plt.savefig(
                os.path.join(debugdir, imname), bbox_inches="tight", pad_inches=0
            )
    elif params["debug"] and MULTI_MODE:
        print("Note: Cannot output debug information in COM multi-mode")

    model.fit(
        ims_train,
        y_train,
        validation_data=(ims_valid, y_valid),
        batch_size=params["BATCH_SIZE"] * ncams,
        epochs=params["EPOCHS"],
        callbacks=[csvlog, model_checkpoint, tboard],
        shuffle=True,
    )

    if params["debug"] and not MULTI_MODE:
        # Plot predictions on validation frames
        debugdir = os.path.join(params["com_train_dir"], "debug_im_out_valid")
        print("Saving debug images to: " + debugdir)
        if not os.path.exists(debugdir):
            os.makedirs(debugdir)

        plt.figure()
        for i in range(ims_valid.shape[0]):
            plt.cla()
            processing.plot_markers_2d(
                processing.norm_im(ims_valid[i]),
                model.predict(ims_valid[i : i + 1])[0],
                newfig=False,
            )
            plt.gca().xaxis.set_major_locator(plt.NullLocator())
            plt.gca().yaxis.set_major_locator(plt.NullLocator())

            imname = str(i) + ".png"
            plt.savefig(
                os.path.join(debugdir, imname), bbox_inches="tight", pad_inches=0
            )
    elif params["debug"] and MULTI_MODE:
        print("Note: Cannot output debug information in COM multi-mode")

    print("Saving full model at end of training")
    sdir = os.path.join(params["com_train_dir"], "fullmodel_weights")
    if not os.path.exists(sdir):
        os.makedirs(sdir)
    model.save(os.path.join(sdir, "fullmodel_end.hdf5"))


def dannce_train(base_config_path):
    """Entrypoint for dannce training."""
    # Set up parameters
    base_params = processing.read_config(base_config_path)
    base_params = processing.make_paths_safe(base_params)

    params = processing.read_config(base_params["io_config"])
    params = processing.make_paths_safe(params)
    params = processing.inherit_config(params, base_params, list(base_params.keys()))

    params["loss"] = getattr(losses, params["loss"])
    params["net"] = getattr(nets, params["net"])

    # Default to 6 views but a smaller number of views can be specified in the DANNCE config.
    # If the legnth of the camera files list is smaller than _N_VIEWS, relevant lists will be
    # duplicated in order to match _N_VIEWS, if possible.
    _N_VIEWS = int(params["_N_VIEWS"] if "_N_VIEWS" in params.keys() else 6)

    # Convert all metric strings to objects
    metrics = []
    for m in params["metric"]:
        try:
            m_obj = getattr(losses, m)
        except AttributeError:
            m_obj = getattr(keras.losses, m)
        metrics.append(m_obj)

    # set GPU ID
    os.environ["CUDA_VISIBLE_DEVICES"] = params["gpuID"]

    # find the weights given config path
    if params["weights"] != "None":
        weights = os.listdir(params["weights"])
        weights = [f for f in weights if ".hdf5" in f]
        weights = weights[0]

        params["weights"] = os.path.join(params["weights"], weights)

        print("Fine-tuning from {}".format(params["weights"]))

    samples = []
    datadict = {}
    datadict_3d = {}
    com3d_dict = {}
    cameras = {}
    camnames = {}
    label3d_files = params["label3d_files"]
    num_experiments = len(label3d_files)
    params["experiment"] = {}
    for e, label3d_file in enumerate(label3d_files):
        exp = params.copy()
        exp = processing.make_paths_safe(exp)
        exp["label3d_file"] = label3d_file
        exp["base_exp_folder"] = os.path.dirname(exp["label3d_file"])
        exp["viddir"] = os.path.join(exp["base_exp_folder"], exp["viddir"])
        for key in ["COMfilename", "COM3D_DICT"]:
            if key in exp:
                exp[key] = os.path.join(base_folder, exp[key])

        if "hard_train" in base_params.keys() and base_params["hard_train"]:
            print("Not duplicating camnames and label3d_files")
        else:
            # If len(exp['CAMNAMES']) divides evenly into 6, duplicate here
            dupes = ["CAMNAMES"]
            for d in dupes:
                val = exp[d]
                if _N_VIEWS % len(val) == 0:
                    num_reps = _N_VIEWS // len(val)
                    exp[d] = val * num_reps
                else:
                    raise Exception(
                        "The length of the {} list must divide evenly into {}.".format(
                            d, _N_VIEWS
                        )
                    )

        (
            samples_,
            datadict_,
            datadict_3d_,
            data_3d_,
            cameras_,
        ) = serve_data_DANNCE.prepare_data(exp)

        # New option: if there is "clean" data (full marker set), can take the
        # 3D COM from the labels
        if "COM_fromlabels" in exp.keys() and exp["COM_fromlabels"]:
            print("For experiment {}, calculating 3D COM from labels".format(e))
            com3d_dict_ = deepcopy(datadict_3d_)
            for key in com3d_dict_.keys():
                com3d_dict_[key] = np.nanmean(datadict_3d_[key], axis=1, keepdims=True)
        else:  # then do traditional COM and sample alignment
            if "COM3D_DICT" not in exp.keys():
                if "COMfilename" not in exp.keys():
                    raise Exception(
                        "The COMfilename or COM3D_DICT field must be populated in the",
                        "yaml for experiment {}".format(e),
                    )

                comfn = exp["COMfilename"]

                datadict_, com3d_dict_ = serve_data_DANNCE.prepare_COM(
                    comfn,
                    datadict_,
                    comthresh=params["comthresh"],
                    weighted=params["weighted"],
                    retriangulate=params["retriangulate"]
                    if "retriangulate" in params.keys()
                    else True,
                    camera_mats=cameras_,
                    method=params["com_method"],
                )

                # Need to cap this at the number of samples included in our
                # COM finding estimates

                tff = list(com3d_dict_.keys())
                samples_ = samples_[: len(tff)]
                data_3d_ = data_3d_[: len(tff)]
                pre = len(samples_)
                samples_, data_3d_ = serve_data_DANNCE.remove_samples_com(
                    samples_,
                    data_3d_,
                    com3d_dict_,
                    rmc=True,
                    cthresh=params["cthresh"],
                )
                msg = "Detected {} bad COMs and removed the associated frames from the dataset"
                print(msg.format(pre - len(samples_)))

            else:
                print(
                    "Loading 3D COM and samples from file: {}".format(exp["COM3D_DICT"])
                )
                c3dfile = sio.loadmat(exp["COM3D_DICT"])
                c3d = c3dfile["com"]
                c3dsi = np.squeeze(c3dfile["sampleID"])
                com3d_dict_ = {}
                for (i, s) in enumerate(c3dsi):
                    com3d_dict_[s] = c3d[i]

                # verify all of the datadict_ keys are in this sample set
                assert (set(c3dsi) & set(list(datadict_.keys()))) == set(
                    list(datadict_.keys())
                )

        print("Using {} samples total.".format(len(samples_)))

        samples, datadict, datadict_3d, com3d_dict = serve_data_DANNCE.add_experiment(
            e,
            samples,
            datadict,
            datadict_3d,
            com3d_dict,
            samples_,
            datadict_,
            datadict_3d_,
            com3d_dict_,
        )

        cameras[e] = cameras_
        camnames[e] = exp["CAMNAMES"]
        print("Using the following cameras: {}".format(camnames[e]))
        params["experiment"][e] = exp

    dannce_train_dir = params["dannce_train_dir"]
    print(dannce_train_dir)

    if not os.path.exists(dannce_train_dir):
        os.makedirs(dannce_train_dir)

    # Additionally, to keep videos unique across experiments, need to add
    # experiment labels in other places. E.g. experiment 0 CameraE's "camname"
    # Becomes 0_CameraE.
    cameras, datadict, params = serve_data_DANNCE.prepend_experiment(
        params, datadict, num_experiments, camnames, cameras
    )

    samples = np.array(samples)

    # Initialize video objects
    vids = {}
    for e in range(num_experiments):
        if params["IMMODE"] == "vid":
            vids = processing.initialize_vids_train(
                params, datadict, e, vids, pathonly=True
            )

    # Parameters
    if params["EXPVAL"]:
        outmode = "coordinates"
    else:
        outmode = "3dprob"

    gridsize = (
        params["NVOX"],
        params["NVOX"],
        params["NVOX"],
    )

    # When this true, the data generator will shuffle the cameras and then select the first 3,
    # to feed to a native 3 camera model
    if "cam3_train" in params.keys() and params["cam3_train"]:
        cam3_train = True
    else:
        cam3_train = False

    valid_params = {
        "dim_in": (
            params["CROP_HEIGHT"][1] - params["CROP_HEIGHT"][0],
            params["CROP_WIDTH"][1] - params["CROP_WIDTH"][0],
        ),
        "n_channels_in": params["N_CHANNELS_IN"],
        "batch_size": 1,
        "n_channels_out": params["NEW_N_CHANNELS_OUT"],
        "out_scale": params["SIGMA"],
        "crop_width": params["CROP_WIDTH"],
        "crop_height": params["CROP_HEIGHT"],
        "vmin": params["VMIN"],
        "vmax": params["VMAX"],
        "nvox": params["NVOX"],
        "interp": params["INTERP"],
        "depth": params["DEPTH"],
        "channel_combo": params["CHANNEL_COMBO"],
        "mode": outmode,
        "camnames": camnames,
        "immode": params["IMMODE"],
        "shuffle": False,  # We will shuffle later
        "rotation": False,  # We will rotate later if desired
        "vidreaders": vids,
        "distort": params["DISTORT"],
        "expval": params["EXPVAL"],
        "crop_im": False,
        "chunks": params["chunks"],
        "preload": False,
    }

    # Setup a generator that will read videos and labels
    tifdirs = []  # Training from single images not yet supported in this demo

    partition = {}
    if "load_valid" not in params.keys():
        all_inds = np.arange(len(samples))

        # extract random inds from each set for validation
        v = params["num_validation_per_exp"]
        valid_inds = []

        if params["num_validation_per_exp"] > 0:  # if 0, do not perform validation
            for e in range(num_experiments):
                tinds = [
                    i for i in range(len(samples)) if int(samples[i].split("_")[0]) == e
                ]
                valid_inds = valid_inds + list(
                    np.random.choice(tinds, (v,), replace=False)
                )

        train_inds = [i for i in all_inds if i not in valid_inds]

        assert (set(valid_inds) & set(train_inds)) == set()

        partition["valid_sampleIDs"] = samples[valid_inds]
        partition["train_sampleIDs"] = samples[train_inds]

        # Save train/val inds
        with open(dannce_train_dir + "val_samples.pickle", "wb") as f:
            cPickle.dump(partition["valid_sampleIDs"], f)

        with open(dannce_train_dir + "train_samples.pickle", "wb") as f:
            cPickle.dump(partition["train_sampleIDs"], f)
    else:
        # Load validation samples from elsewhere
        with open(os.path.join(params["load_valid"], "val_samples.pickle"), "rb",) as f:
            partition["valid_sampleIDs"] = cPickle.load(f)
        partition["train_sampleIDs"] = [
            f for f in samples if f not in partition["valid_sampleIDs"]
        ]

    print(cameras)
    train_generator = DataGenerator_3Dconv(
        partition["train_sampleIDs"],
        datadict,
        datadict_3d,
        cameras,
        partition["train_sampleIDs"],
        com3d_dict,
        tifdirs,
        **valid_params
    )
    valid_generator = DataGenerator_3Dconv(
        partition["valid_sampleIDs"],
        datadict,
        datadict_3d,
        cameras,
        partition["valid_sampleIDs"],
        com3d_dict,
        tifdirs,
        **valid_params
    )

    # We should be able to load everything into memory...
    X_train = np.zeros(
        (
            len(partition["train_sampleIDs"]),
            params["NVOX"],
            params["NVOX"],
            params["NVOX"],
            params["N_CHANNELS_IN"] * len(camnames[0]),
        ),
        dtype="float32",
    )

    X_valid = np.zeros(
        (
            len(partition["valid_sampleIDs"]),
            params["NVOX"],
            params["NVOX"],
            params["NVOX"],
            params["N_CHANNELS_IN"] * len(camnames[0]),
        ),
        dtype="float32",
    )

    X_train_grid = None
    X_valid_grid = None
    if params["EXPVAL"]:
        y_train = np.zeros(
            (len(partition["train_sampleIDs"]), 3, params["NEW_N_CHANNELS_OUT"],),
            dtype="float32",
        )
        X_train_grid = np.zeros(
            (len(partition["train_sampleIDs"]), params["NVOX"] ** 3, 3),
            dtype="float32",
        )

        y_valid = np.zeros(
            (len(partition["valid_sampleIDs"]), 3, params["NEW_N_CHANNELS_OUT"],),
            dtype="float32",
        )
        X_valid_grid = np.zeros(
            (len(partition["valid_sampleIDs"]), params["NVOX"] ** 3, 3),
            dtype="float32",
        )
    else:
        y_train = np.zeros(
            (
                len(partition["train_sampleIDs"]),
                params["NVOX"],
                params["NVOX"],
                params["NVOX"],
                params["NEW_N_CHANNELS_OUT"],
            ),
            dtype="float32",
        )

        y_valid = np.zeros(
            (
                len(partition["valid_sampleIDs"]),
                params["NVOX"],
                params["NVOX"],
                params["NVOX"],
                params["NEW_N_CHANNELS_OUT"],
            ),
            dtype="float32",
        )

    print(
        "Loading training data into memory. This can take a while to seek through",
        "large sets of video. This process is much faster if the frame indices",
        "are sorted in ascending order in your label data file.",
    )
    for i in range(len(partition["train_sampleIDs"])):
        print(i, end="\r")
        rr = train_generator.__getitem__(i)
        if params["EXPVAL"]:
            X_train[i] = rr[0][0]
            X_train_grid[i] = rr[0][1]
        else:
            X_train[i] = rr[0]
        y_train[i] = rr[1]

    # tifdir = '/n/holylfs02/LABS/olveczky_lab/Jesse/P20_pups/RecordingP20Pup_one/images'
    # for i in range(X_train.shape[0]):
    #    for j in range(len(camnames[0])):
    #        im = X_train[i,:,:,:,j*3:(j+1)*3]
    #        im = processing.norm_im(im)*255
    #        im = im.astype('uint8')
    #        of = os.path.join(tifdir,partition['train_sampleIDs'][i]+'_cam' + str(j) + '.tif')
    #        imageio.mimwrite(of,np.transpose(im,[2,0,1,3]))
    # sys.exit()

    print("Loading validation data into memory")
    for i in range(len(partition["valid_sampleIDs"])):
        print(i, end="\r")
        rr = valid_generator.__getitem__(i)
        if params["EXPVAL"]:
            X_valid[i] = rr[0][0]
            X_valid_grid[i] = rr[0][1]
        else:
            X_valid[i] = rr[0]
        y_valid[i] = rr[1]

    # Now we can generate from memory with shuffling, rotation, etc.
    if params["CHANNEL_COMBO"] == "random":
        randflag = True
    else:
        randflag = False

    train_generator = DataGenerator_3Dconv_frommem(
        np.arange(len(partition["train_sampleIDs"])),
        X_train,
        y_train,
        batch_size=params["BATCH_SIZE"],
        random=randflag,
        rotation=params["ROTATE"],
        expval=params["EXPVAL"],
        xgrid=X_train_grid,
        nvox=params["NVOX"],
        cam3_train=cam3_train,
    )
    valid_generator = DataGenerator_3Dconv_frommem(
        np.arange(len(partition["valid_sampleIDs"])),
        X_valid,
        y_valid,
        batch_size=1,
        random=randflag,
        rotation=False,
        expval=params["EXPVAL"],
        xgrid=X_valid_grid,
        nvox=params["NVOX"],
        shuffle=False,
        cam3_train=cam3_train,
    )

    # Build net
    print("Initializing Network...")

    assert not (params["batch_norm"] == True) & (params["instance_norm"] == True)

    # Currently, we expect four modes of use:
    # 1) Training a new network from scratch
    # 2) Fine-tuning a network trained on a diff. dataset (transfer learning)
    # 3) Continuing to train 1) or 2) from a full model checkpoint (including optimizer state)

    print("NUM CAMERAS: {}".format(len(camnames[0])))

    if params["train_mode"] == "new":
        model = params["net"](
            params["loss"],
            float(params["lr"]),
            params["N_CHANNELS_IN"] + params["DEPTH"],
            params["N_CHANNELS_OUT"],
            len(camnames[0]),
            batch_norm=params["batch_norm"],
            instance_norm=params["instance_norm"],
            include_top=True,
            gridsize=gridsize,
        )
    elif params["train_mode"] == "finetune":
        model = params["net"](
            params["loss"],
            float(params["lr"]),
            params["N_CHANNELS_IN"] + params["DEPTH"],
            params["N_CHANNELS_OUT"],
            len(camnames[0]),
            params["NEW_LAST_KERNEL_SIZE"],
            params["NEW_N_CHANNELS_OUT"],
            params["weights"],
            params["N_LAYERS_LOCKED"],
            batch_norm=params["batch_norm"],
            instance_norm=params["instance_norm"],
            gridsize=gridsize,
        )
    elif params["train_mode"] == "continued":
        model = load_model(
            params["weights"],
            custom_objects={
                "ops": ops,
                "slice_input": nets.slice_input,
                "mask_nan_keep_loss": losses.mask_nan_keep_loss,
                "euclidean_distance_3D": losses.euclidean_distance_3D,
                "centered_euclidean_distance_3D": losses.centered_euclidean_distance_3D,
            },
        )
    elif params["train_mode"] == "continued_weights_only":
        # This does not work with models created in 'finetune' mode, but will work with models
        # started from scratch ('new' train_mode)
        model = params["net"](
            params["loss"],
            float(params["lr"]),
            params["N_CHANNELS_IN"] + params["DEPTH"],
            params["N_CHANNELS_OUT"],
            3 if cam3_train else len(camnames[0]),
            batch_norm=params["batch_norm"],
            instance_norm=params["instance_norm"],
            include_top=True,
            gridsize=gridsize,
        )
        model.load_weights(params["weights"])
    else:
        raise Exception("Invalid training mode")

    model.compile(
        optimizer=Adam(lr=float(params["lr"])), loss=params["loss"], metrics=metrics,
    )

    print("COMPLETE\n")

    # Create checkpoint and logging callbacks
    if params["num_validation_per_exp"] > 0:
        kkey = "weights.{epoch:02d}-{val_loss:.5f}.hdf5"
        mon = "val_loss"
    else:
        kkey = "weights.{epoch:02d}-{loss:.5f}.hdf5"
        mon = "loss"

    model_checkpoint = ModelCheckpoint(
        os.path.join(dannce_train_dir, kkey),
        monitor=mon,
        save_best_only=True,
        save_weights_only=True,
    )
    csvlog = CSVLogger(os.path.join(dannce_train_dir, "training.csv"))
    tboard = TensorBoard(
        log_dir=dannce_train_dir + "logs", write_graph=False, update_freq=100
    )

    model.fit(
        x=train_generator,
        steps_per_epoch=len(train_generator),
        validation_data=valid_generator,
        validation_steps=len(valid_generator),
        verbose=params["VERBOSE"],
        epochs=params["EPOCHS"],
        callbacks=[csvlog, model_checkpoint, tboard],
    )

    print("Saving full model at end of training")
    sdir = os.path.join(params["dannce_train_dir"], "fullmodel_weights")
    if not os.path.exists(sdir):
        os.makedirs(sdir)
    model.save(os.path.join(sdir, "fullmodel_end.hdf5"))

    print("done!")


def dannce_predict(base_config_path):
    # Set up parameters
    base_params = processing.read_config(base_config_path)
    base_params = processing.make_paths_safe(base_params)
    params = processing.read_config(base_params["io_config"])
    params = processing.make_paths_safe(params)
    params = processing.inherit_config(params, base_params, list(base_params.keys()))

    # Load the appropriate loss function and network
    try:
        params["loss"] = getattr(losses, params["loss"])
    except AttributeError:
        params["loss"] = getattr(keras_losses, params["loss"])
    netname = params["net"]
    params["net"] = getattr(nets, params["net"])

    # While we can use experiment files for DANNCE training,
    # for prediction we use the base data files present in the main config
    # Grab the input file for prediction
    params["label3d_file"] = processing.grab_predict_label3d_file()
    base_folder = os.path.dirname(params["label3d_file"])
    for key in ["COMfilename", "COM3D_DICT"]:
        if key in params:
            params[key] = os.path.join(base_folder, params[key])

    # Also add parent params under the 'experiment' key for compatibility
    # with DANNCE's video loading function
    params["experiment"] = params

    dannce_predict_dir = params["dannce_predict_dir"]
    print(dannce_predict_dir)

    if not os.path.exists(dannce_predict_dir):
        os.makedirs(dannce_predict_dir)

    # default to slow numpy backend if there is no rpedict_mode in config file. I.e. legacy support
    predict_mode = (
        params["predict_mode"] if "predict_mode" in params.keys() else "numpy"
    )
    print("Using {} predict mode".format(predict_mode))

    # Copy the configs into the dannce_predict_dir, for reproducibility
    processing.copy_config(
        dannce_predict_dir,
        sys.argv[1],
        base_params["io_config"],
        base_params["io_config"],
    )

    # Default to 6 views but a smaller number of views can be specified in the DANNCE config.
    # If the legnth of the camera files list is smaller than _N_VIEWS, relevant lists will be
    # duplicated in order to match _N_VIEWS, if possible.
    _N_VIEWS = int(params["_N_VIEWS"] if "_N_VIEWS" in params.keys() else 6)

    os.environ["CUDA_VISIBLE_DEVICES"] = params["gpuID"]
    gpuID = params["gpuID"]

    # If len(params['experiment']['CAMNAMES']) divides evenly into 6, duplicate here,
    # Unless the network was "hard" trained to use less than 6 cameras
    if "hard_train" in base_params.keys() and base_params["hard_train"]:
        print("Not duplicating camnames and label3d_files")
    else:
        dupes = ["CAMNAMES"]
        for d in dupes:
            val = params["experiment"][d]
            if _N_VIEWS % len(val) == 0:
                num_reps = _N_VIEWS // len(val)
                params["experiment"][d] = val * num_reps
            else:
                raise Exception(
                    "The length of the {} list must divide evenly into {}.".format(
                        d, _N_VIEWS
                    )
                )

    (
        samples_,
        datadict_,
        datadict_3d_,
        data_3d_,
        cameras_,
    ) = serve_data_DANNCE.prepare_data(params["experiment"], prediction=True)
    if "COM3D_DICT" not in params.keys():

        # Load in the COM file at the default location, or use one in the config file if provided
        if "COMfilename" in params.keys():
            comfn = params["COMfilename"]
        else:
            raise Exception(
                "Please define either COM3D_DICT or COMfilename in exp.yaml"
            )

        datadict_, com3d_dict_ = serve_data_DANNCE.prepare_COM(
            comfn,
            datadict_,
            comthresh=params["comthresh"],
            weighted=params["weighted"],
            retriangulate=params["retriangulate"]
            if "retriangulate" in params.keys()
            else True,
            camera_mats=dcameras_
            if "allcams" in params.keys() and params["allcams"]
            else cameras_,
            method=params["com_method"],
            allcams=params["allcams"]
            if "allcams" in params.keys() and params["allcams"]
            else False,
        )

        # Need to cap this at the number of samples included in our
        # COM finding estimates
        tff = list(com3d_dict_.keys())
        samples_ = samples_[: len(tff)]
        data_3d_ = data_3d_[: len(tff)]
        pre = len(samples_)
        samples_, data_3d_ = serve_data_DANNCE.remove_samples_com(
            samples_, data_3d_, com3d_dict_, rmc=True, cthresh=params["cthresh"],
        )
        msg = "Detected {} bad COMs and removed the associated frames from the dataset"
        print(msg.format(pre - len(samples_)))

    else:

        print("Loading 3D COM and samples from file: {}".format(exp["COM3D_DICT"]))
        c3dfile = sio.loadmat(exp["COM3D_DICT"])
        c3d = c3dfile["com"]
        c3dsi = np.squeeze(c3dfile["sampleID"])
        com3d_dict_ = {}
        for (i, s) in enumerate(c3dsi):
            com3d_dict_[s] = c3d[i]

        # verify all of these samples are in datadict_, which we require in order to get the frames IDs
        # for the videos
        assert (set(c3dsi) & set(list(datadict_.keys()))) == set(list(datadict_.keys()))

    # Write 3D COM to file
    cfilename = os.path.join(dannce_predict_dir, "COM3D_undistorted.mat")
    print("Saving 3D COM to {}".format(cfilename))
    c3d = np.zeros((len(samples_), 3))
    for i in range(len(samples_)):
        c3d[i] = com3d_dict_[samples_[i]]
    sio.savemat(cfilename, {"sampleID": samples_, "com": c3d})

    # The library is configured to be able to train over multiple animals ("experiments")
    # at once. Because supporting code expects to see an experiment ID# prepended to
    # each of these data keys, we need to add a token experiment ID here.
    samples = []
    datadict = {}
    datadict_3d = {}
    com3d_dict = {}
    samples, datadict, datadict_3d, com3d_dict = serve_data_DANNCE.add_experiment(
        0,
        samples,
        datadict,
        datadict_3d,
        com3d_dict,
        samples_,
        datadict_,
        datadict_3d_,
        com3d_dict_,
    )
    cameras = {}
    cameras[0] = cameras_
    camnames = {}
    camnames[0] = params["experiment"]["CAMNAMES"]
    samples = np.array(samples)
    # Initialize video dictionary. paths to videos only.
    if params["IMMODE"] == "vid":
        vids = processing.initialize_vids(params, datadict, pathonly=True)

    # Parameters
    valid_params = {
        "dim_in": (
            params["CROP_HEIGHT"][1] - params["CROP_HEIGHT"][0],
            params["CROP_WIDTH"][1] - params["CROP_WIDTH"][0],
        ),
        "n_channels_in": params["N_CHANNELS_IN"],
        "batch_size": params["BATCH_SIZE"],
        "n_channels_out": params["N_CHANNELS_OUT"],
        "out_scale": params["SIGMA"],
        "crop_width": params["CROP_WIDTH"],
        "crop_height": params["CROP_HEIGHT"],
        "vmin": params["VMIN"],
        "vmax": params["VMAX"],
        "nvox": params["NVOX"],
        "interp": params["INTERP"],
        "depth": params["DEPTH"],
        "channel_combo": params["CHANNEL_COMBO"],
        "mode": "coordinates",
        "camnames": camnames,
        "immode": params["IMMODE"],
        "shuffle": False,
        "rotation": False,
        "vidreaders": vids,
        "distort": params["DISTORT"],
        "expval": params["EXPVAL"],
        "crop_im": False,
        "chunks": params["chunks"],
        "preload": False,
    }

    # Datasets
    partition = {}
    valid_inds = np.arange(len(samples))
    partition["valid_sampleIDs"] = samples[valid_inds]
    tifdirs = []

    # Generators
    if predict_mode == "torch":
        import torch

        device = "cuda:" + gpuID
        valid_generator = DataGenerator_3Dconv_torch(
            partition["valid_sampleIDs"],
            datadict,
            datadict_3d,
            cameras,
            partition["valid_sampleIDs"],
            com3d_dict,
            tifdirs,
            **valid_params
        )
    elif predict_mode == "tf":
        device = "/GPU:" + gpuID
        valid_generator = DataGenerator_3Dconv_tf(
            partition["valid_sampleIDs"],
            datadict,
            datadict_3d,
            cameras,
            partition["valid_sampleIDs"],
            com3d_dict,
            tifdirs,
            **valid_params
        )
    else:
        valid_generator = DataGenerator_3Dconv(
            partition["valid_sampleIDs"],
            datadict,
            datadict_3d,
            cameras,
            partition["valid_sampleIDs"],
            com3d_dict,
            tifdirs,
            **valid_params
        )

    # Build net
    print("Initializing Network...")

    # This requires that the network be saved as a full model, not just weights.
    # As a precaution, we import all possible custom objects that could be used
    # by a model and thus need declarations

    if "predict_model" in params.keys():
        mdl_file = params["predict_model"]
    else:
        wdir = params["dannce_train_dir"]
        weights = os.listdir(wdir)
        weights = [f for f in weights if ".hdf5" in f]
        weights = sorted(weights, key=lambda x: int(x.split(".")[1].split("-")[0]))
        weights = weights[-1]

        mdl_file = os.path.join(wdir, weights)
        print("Loading model from " + mdl_file)

    if (
        netname == "unet3d_big_tiedfirstlayer_expectedvalue"
        or "FROM_WEIGHTS" in params.keys()
    ):
        # This network is too "custom" to be loaded in as a full model, until I
        # figure out how to unroll the first tied weights layer
        gridsize = (
            params["NVOX"],
            params["NVOX"],
            params["NVOX"],
        )
        model = params["net"](
            params["loss"],
            float(params["lr"]),
            params["N_CHANNELS_IN"] + params["DEPTH"],
            params["N_CHANNELS_OUT"],
            len(camnames[0]),
            batch_norm=params["batch_norm"],
            instance_norm=params["instance_norm"],
            include_top=True,
            gridsize=gridsize,
        )
        model.load_weights(mdl_file)
    else:
        model = load_model(
            mdl_file,
            custom_objects={
                "ops": ops,
                "slice_input": nets.slice_input,
                "mask_nan_keep_loss": losses.mask_nan_keep_loss,
                "euclidean_distance_3D": losses.euclidean_distance_3D,
                "centered_euclidean_distance_3D": losses.centered_euclidean_distance_3D,
            },
        )

    # To speed up EXPVAL prediction, rather than doing two forward passes: one for the 3d coordinate
    # and one for the probability map, here we splice on a new output layer after
    # the softmax on the last convolutional layer
    if params["EXPVAL"]:
        from tensorflow.keras.layers import GlobalMaxPooling3D

        o2 = GlobalMaxPooling3D()(model.layers[-3].output)
        model = Model(
            inputs=[model.layers[0].input, model.layers[-2].input],
            outputs=[model.layers[-1].output, o2],
        )

    save_data = {}

    def evaluate_ondemand(start_ind, end_ind, valid_gen):
        """Evaluate experiment.
        :param start_ind: Starting frame
        :param end_ind: Ending frame
        :param valid_gen: Generator
        """
        end_time = time.time()
        for i in range(start_ind, end_ind):
            print("Predicting on batch {}".format(i), flush=True)
            if (i - start_ind) % 10 == 0 and i != start_ind:
                print(i)
                print("10 batches took {} seconds".format(time.time() - end_time))
                end_time = time.time()

            if (i - start_ind) % 1000 == 0 and i != start_ind:
                print("Saving checkpoint at {}th batch".format(i))
                if params["EXPVAL"]:
                    p_n = savedata_expval(
                        dannce_predict_dir + "save_data_AVG.mat",
                        write=True,
                        data=save_data,
                        tcoord=False,
                        num_markers=nchn,
                        pmax=True,
                    )
                else:
                    p_n = savedata_tomat(
                        dannce_predict_dir + "save_data_MAX.mat",
                        params["VMIN"],
                        params["VMAX"],
                        params["NVOX"],
                        write=True,
                        data=save_data,
                        num_markers=nchn,
                        tcoord=False,
                    )

            ims = valid_gen.__getitem__(i)
            pred = model.predict(ims[0])

            if params["EXPVAL"]:
                probmap = pred[1]
                pred = pred[0]
                for j in range(pred.shape[0]):
                    pred_max = probmap[j]
                    sampleID = partition["valid_sampleIDs"][i * pred.shape[0] + j]
                    save_data[i * pred.shape[0] + j] = {
                        "pred_max": pred_max,
                        "pred_coord": pred[j],
                        "sampleID": sampleID,
                    }
            else:
                if predict_mode == "torch":
                    for j in range(pred.shape[0]):
                        preds = torch.as_tensor(
                            pred[j], dtype=torch.float32, device=device
                        )
                        pred_max = preds.max(0).values.max(0).values.max(0).values
                        pred_total = preds.sum((0, 1, 2))
                        xcoord, ycoord, zcoord = processing.plot_markers_3d_torch(preds)
                        coord = torch.stack([xcoord, ycoord, zcoord])
                        pred_log = pred_max.log() - pred_total.log()
                        sampleID = partition["valid_sampleIDs"][i * pred.shape[0] + j]

                        save_data[i * pred.shape[0] + j] = {
                            "pred_max": pred_max.cpu().numpy(),
                            "pred_coord": coord.cpu().numpy(),
                            "true_coord_nogrid": ims[1][j],
                            "logmax": pred_log.cpu().numpy(),
                            "sampleID": sampleID,
                        }

                elif predict_mode == "tf":
                    # get coords for each map
                    with tf.device(device):
                        for j in range(pred.shape[0]):
                            preds = tf.constant(pred[j], dtype="float32")
                            pred_max = tf.math.reduce_max(
                                tf.math.reduce_max(tf.math.reduce_max(preds))
                            )
                            pred_total = tf.math.reduce_sum(
                                tf.math.reduce_sum(tf.math.reduce_sum(preds))
                            )
                            xcoord, ycoord, zcoord = processing.plot_markers_3d_tf(
                                preds
                            )
                            coord = tf.stack([xcoord, ycoord, zcoord], axis=0)
                            pred_log = tf.math.log(pred_max) - tf.math.log(pred_total)
                            sampleID = partition["valid_sampleIDs"][
                                i * pred.shape[0] + j
                            ]

                            save_data[i * pred.shape[0] + j] = {
                                "pred_max": pred_max.numpy(),
                                "pred_coord": coord.numpy(),
                                "true_coord_nogrid": ims[1][j],
                                "logmax": pred_log.numpy(),
                                "sampleID": sampleID,
                            }

                else:
                    # get coords for each map
                    for j in range(pred.shape[0]):
                        pred_max = np.max(pred[j], axis=(0, 1, 2))
                        pred_total = np.sum(pred[j], axis=(0, 1, 2))
                        xcoord, ycoord, zcoord = processing.plot_markers_3d(
                            pred[j, :, :, :, :]
                        )
                        coord = np.stack([xcoord, ycoord, zcoord])
                        pred_log = np.log(pred_max) - np.log(pred_total)
                        sampleID = partition["valid_sampleIDs"][i * pred.shape[0] + j]

                        save_data[i * pred.shape[0] + j] = {
                            "pred_max": pred_max,
                            "pred_coord": coord,
                            "true_coord_nogrid": ims[1][j],
                            "logmax": pred_log,
                            "sampleID": sampleID,
                        }

    max_eval_batch = params["maxbatch"]
    print(max_eval_batch)
    if max_eval_batch == "max":
        max_eval_batch = len(valid_generator)

    if "NEW_N_CHANNELS_OUT" in params.keys():
        nchn = params["NEW_N_CHANNELS_OUT"]
    else:
        nchn = params["N_CHANNELS_OUT"]

    evaluate_ondemand(0, max_eval_batch, valid_generator)

    if params["EXPVAL"]:
        p_n = savedata_expval(
            dannce_predict_dir + "save_data_AVG.mat",
            write=True,
            data=save_data,
            tcoord=False,
            num_markers=nchn,
            pmax=True,
        )
    else:
        p_n = savedata_tomat(
            dannce_predict_dir + "save_data_MAX.mat",
            params["VMIN"],
            params["VMAX"],
            params["NVOX"],
            write=True,
            data=save_data,
            num_markers=nchn,
            tcoord=False,
        )

    print("done!")
