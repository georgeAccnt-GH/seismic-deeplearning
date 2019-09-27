"""Train models on Dutch F3 salt dataset

Trains models using PyTorch DistributedDataParallel
Uses a warmup schedule that then goes into a cyclic learning rate
"""
# /* spell-checker: disable */

import logging
import logging.config
import os
from os import path

import cv2
import fire
import numpy as np
import torch
from albumentations import (
    Compose,
    HorizontalFlip,
    Normalize,
    Resize,
    PadIfNeeded,
)
from cv_lib.event_handlers import (
    SnapshotHandler,
    logging_handlers,
    tensorboard_handlers,
)
from cv_lib.event_handlers.logging_handlers import Evaluator
from cv_lib.event_handlers.tensorboard_handlers import (
    create_image_writer,
    create_summary_writer,
)
from cv_lib.segmentation import models
from deepseismic_interpretation.dutchf3.data import (
    get_patch_loader,
    decode_segmap,
)
from cv_lib.segmentation.dutchf3.engine import (
    create_supervised_evaluator,
    create_supervised_trainer,
)
from cv_lib.segmentation.dutchf3.metrics import apex
from cv_lib.segmentation.dutchf3.utils import (
    current_datetime,
    generate_path,
    git_branch,
    git_hash,
    np_to_tb,
)
from default import _C as config
from default import update_config
from ignite.contrib.handlers import (
    ConcatScheduler,
    CosineAnnealingScheduler,
    LinearCyclicalScheduler,
)
from ignite.engine import Events
from ignite.utils import convert_tensor
from toolz import compose, curry
from torch.utils import data


def prepare_batch(batch, device=None, non_blocking=False):
    x, y = batch
    return (
        convert_tensor(x, device=device, non_blocking=non_blocking),
        convert_tensor(y, device=device, non_blocking=non_blocking),
    )


@curry
def update_sampler_epoch(data_loader, engine):
    data_loader.sampler.epoch = engine.state.epoch


def run(*options, cfg=None, local_rank=0):
    """Run training and validation of model

    Notes:
        Options can be passed in via the options argument and loaded from the cfg file
        Options loaded from default.py will be overridden by options loaded from cfg file
        Options passed in through options argument will override option loaded from cfg file
    
    Args:
        *options (str,int ,optional): Options used to overide what is loaded from the config. 
                                      To see what options are available consult default.py
        cfg (str, optional): Location of config file to load. Defaults to None.
    """
    update_config(config, options=options, config_file=cfg)
    logging.config.fileConfig(config.LOG_CONFIG)
    logger = logging.getLogger(__name__)
    logger.debug(config.WORKERS)
    silence_other_ranks = True
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    distributed = world_size > 1

    if distributed:
        # FOR DISTRIBUTED:  Set the device according to local_rank.
        torch.cuda.set_device(local_rank)

        # FOR DISTRIBUTED:  Initialize the backend.  torch.distributed.launch will provide
        # environment variables, and requires that you use init_method=`env://`.
        torch.distributed.init_process_group(
            backend="nccl", init_method="env://"
        )

    scheduler_step = config.TRAIN.END_EPOCH // config.TRAIN.SNAPSHOTS
    torch.backends.cudnn.benchmark = config.CUDNN.BENCHMARK

    torch.manual_seed(config.SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(config.SEED)
    np.random.seed(seed=config.SEED)
    # Setup Augmentations
    basic_aug = Compose(
        [
            Normalize(
                mean=(config.TRAIN.MEAN,),
                std=(config.TRAIN.STD,),
                max_pixel_value=1,
            ),
            Resize(
                config.TRAIN.AUGMENTATIONS.RESIZE.HEIGHT,
                config.TRAIN.AUGMENTATIONS.RESIZE.WIDTH,
                always_apply=True,
            ),
            PadIfNeeded(
                min_height=config.TRAIN.AUGMENTATIONS.PAD.HEIGHT,
                min_width=config.TRAIN.AUGMENTATIONS.PAD.WIDTH,
                border_mode=cv2.BORDER_CONSTANT,
                always_apply=True,
                mask_value=255,
            ),
        ]
    )
    if config.TRAIN.AUGMENTATION:
        train_aug = Compose(
            [
                basic_aug,
                HorizontalFlip(p=0.5),
            ]
        )
        val_aug = basic_aug
    else:
        train_aug = val_aug = basic_aug

    TrainPatchLoader = get_patch_loader(config)

    train_set = TrainPatchLoader(
        config.DATASET.ROOT,
        split="train",
        is_transform=True,
        stride=config.TRAIN.STRIDE,
        patch_size=config.TRAIN.PATCH_SIZE,
        augmentations=train_aug,
    )
    logger.info(f"Training examples {len(train_set)}")

    val_set = TrainPatchLoader(
        config.DATASET.ROOT,
        split="val",
        is_transform=True,
        stride=config.TRAIN.STRIDE,
        patch_size=config.TRAIN.PATCH_SIZE,
        augmentations=val_aug
    )
    logger.info(f"Validation examples {len(val_set)}")
    n_classes = train_set.n_classes

    train_sampler = torch.utils.data.distributed.DistributedSampler(
        train_set, num_replicas=world_size, rank=local_rank
    )

    train_loader = data.DataLoader(
        train_set,
        batch_size=config.TRAIN.BATCH_SIZE_PER_GPU,
        num_workers=config.WORKERS,
        sampler=train_sampler,
    )

    val_sampler = torch.utils.data.distributed.DistributedSampler(
        val_set, num_replicas=world_size, rank=local_rank
    )

    val_loader = data.DataLoader(
        val_set,
        batch_size=config.VALIDATION.BATCH_SIZE_PER_GPU,
        num_workers=config.WORKERS,
        sampler=val_sampler,
    )

    model = getattr(models, config.MODEL.NAME).get_seg_model(config)

    device = "cpu"
    if torch.cuda.is_available():
        device = "cuda"
    model = model.to(device)  # Send to GPU

    optimizer = torch.optim.SGD(
        model.parameters(),
        lr=config.TRAIN.MAX_LR,
        momentum=config.TRAIN.MOMENTUM,
        weight_decay=config.TRAIN.WEIGHT_DECAY,
    )

    # weights are inversely proportional to the frequency of the classes in the training set
    class_weights = torch.tensor(
        config.DATASET.CLASS_WEIGHTS, device=device, requires_grad=False
    )

    criterion = torch.nn.CrossEntropyLoss(
        weight=class_weights, ignore_index=255, reduction="mean"
    )

    model = torch.nn.parallel.DistributedDataParallel(
        model, device_ids=[device], find_unused_parameters=True
    )

    snapshot_duration = scheduler_step * len(train_loader)
    warmup_duration = 5 * len(train_loader)
    warmup_scheduler = LinearCyclicalScheduler(
        optimizer,
        "lr",
        start_value=config.TRAIN.MAX_LR,
        end_value=config.TRAIN.MAX_LR * world_size,
        cycle_size=10 * len(train_loader),
    )
    cosine_scheduler = CosineAnnealingScheduler(
        optimizer,
        "lr",
        config.TRAIN.MAX_LR * world_size,
        config.TRAIN.MIN_LR * world_size,
        snapshot_duration,
    )

    scheduler = ConcatScheduler(
        schedulers=[warmup_scheduler, cosine_scheduler],
        durations=[warmup_duration],
    )

    trainer = create_supervised_trainer(
        model, optimizer, criterion, prepare_batch, device=device
    )

    trainer.add_event_handler(Events.ITERATION_STARTED, scheduler)
    # Set to update the epoch parameter of our distributed data sampler so that we get different shuffles
    trainer.add_event_handler(
        Events.EPOCH_STARTED, update_sampler_epoch(train_loader)
    )

    if silence_other_ranks & local_rank != 0:
        logging.getLogger("ignite.engine.engine.Engine").setLevel(
            logging.WARNING
        )

    def _select_pred_and_mask(model_out_dict):
        return (
            model_out_dict["y_pred"].squeeze(),
            model_out_dict["mask"].squeeze(),
        )
    
    evaluator = create_supervised_evaluator(
        model,
        prepare_batch,
        metrics={
            "IoU": apex.MeanIoU(
                n_classes, device, output_transform=_select_pred_and_mask
            ),
            "nll": salt_metrics.LossMetric(
                criterion,
                world_size,
                config.VALIDATION.BATCH_SIZE_PER_GPU,
                output_transform=_select_pred_and_mask,
            ),
            "mca": apex.MeanClassAccuracy(
                n_classes, device, output_transform=_select_pred_and_mask
            ),
            "fiou": apex.FrequencyWeightedIoU(
                n_classes, device, output_transform=_select_pred_and_mask
            ),
            "pixa": apex.PixelwiseAccuracy(
                n_classes, device, output_transform=_select_pred_and_mask
            ),
        },
        device=device,
    )

    # Set the validation run to start on the epoch completion of the training run
    trainer.add_event_handler(
        Events.EPOCH_COMPLETED, Evaluator(evaluator, val_loader)
    )

    if local_rank == 0:  # Run only on master process

        trainer.add_event_handler(
            Events.ITERATION_COMPLETED,
            logging_handlers.log_training_output(log_interval=config.PRINT_FREQ),
        )
        trainer.add_event_handler(
            Events.EPOCH_STARTED, logging_handlers.log_lr(optimizer)
        )

        output_dir = generate_path(
            config.OUTPUT_DIR,
            git_branch(),
            git_hash(),
            config.MODEL.NAME,
            current_datetime(),
        )
        summary_writer = create_summary_writer(
            log_dir=path.join(output_dir, config.LOG_DIR)
        )
        logger.info(f"Logging Tensorboard to {path.join(output_dir, config.LOG_DIR)}")
        trainer.add_event_handler(
            Events.EPOCH_STARTED,
            tensorboard_handlers.log_lr(summary_writer, optimizer, "epoch"),
        )
        trainer.add_event_handler(
            Events.ITERATION_COMPLETED,
            tensorboard_handlers.log_training_output(summary_writer),
        )
        evaluator.add_event_handler(
            Events.EPOCH_COMPLETED,
            logging_handlers.log_metrics(
                "Validation results",
                metrics_dict={
                    "IoU": "IoU :",
                    "nll": "Avg loss :",
                    "pixa": "Pixelwise Accuracy :",
                    "mca": "Mean Class Accuracy :",
                    "fiou": "Freq Weighted IoU :",  
                },
            ),
        )
        evaluator.add_event_handler(
            Events.EPOCH_COMPLETED,
            tensorboard_handlers.log_metrics(
                summary_writer,
                trainer,
                "epoch",
                metrics_dict={
                    "IoU": "Validation/IoU",
                    "nll": "Validation/Loss",
                    "mca": "Validation/MCA",
                    "fiou": "Validation/FIoU",
                },
            ),
        )

        def _select_max(pred_tensor):
            return pred_tensor.max(1)[1]

        def _tensor_to_numpy(pred_tensor):
            return pred_tensor.squeeze().cpu().numpy()
        
        transform_func = compose(
            np_to_tb, decode_segmap(n_classes=n_classes), _tensor_to_numpy
        )

        transform_pred = compose(transform_func, _select_max)

        evaluator.add_event_handler(
            Events.EPOCH_COMPLETED,
            create_image_writer(summary_writer, "Validation/Image", "image"),
        )
        evaluator.add_event_handler(
            Events.EPOCH_COMPLETED,
            create_image_writer(
                summary_writer,
                "Validation/Mask",
                "mask",
                transform_func=transform_func,
            ),
        )
        evaluator.add_event_handler(
            Events.EPOCH_COMPLETED,
            create_image_writer(
                summary_writer,
                "Validation/Pred",
                "y_pred",
                transform_func=transform_pred,
            ),
        )

        def snapshot_function():
            return (trainer.state.iteration % snapshot_duration) == 0

       
        checkpoint_handler = SnapshotHandler(
            path.join(output_dir, config.TRAIN.MODEL_DIR),
            config.MODEL.NAME,
            snapshot_function,
        )
        evaluator.add_event_handler(
            Events.EPOCH_COMPLETED, checkpoint_handler, {"model": model}
        )

        
        logger.info("Starting training")

    trainer.run(train_loader, max_epochs=config.TRAIN.END_EPOCH)


if __name__ == "__main__":
    fire.Fire(run)