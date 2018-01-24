import os
import inspect
import cv2
import numpy as np
import keras.backend as K
from glob import glob
from keras.utils import Sequence
from keras.callbacks import Callback, ReduceLROnPlateau
from tqdm import tqdm
from abc import abstractmethod
from sklearn.utils.class_weight import compute_sample_weight
from multiprocessing.pool import ThreadPool
from skimage.exposure import adjust_gamma
from numba import jit

LABELS = [
    'HTC-1-M7',
    'iPhone-4s',
    'iPhone-6',
    'LG-Nexus-5x',
    'Motorola-Droid-Maxx',
    'Motorola-Nexus-6',
    'Motorola-X',
    'Samsung-Galaxy-Note3',
    'Samsung-Galaxy-S4',
    'Sony-NEX-7'
]
N_CLASS = len(LABELS)
ROOT_DIR = '..'
TRAIN_DIR = os.path.join(ROOT_DIR, 'data', 'train')
TEST_DIR = os.path.join(ROOT_DIR, 'data', 'test')
ID2LABEL = {i: label for i, label in enumerate(LABELS)}
LABEL2ID = {label: i for i, label in ID2LABEL.items()}
CROP_SIDE = 512

# change built-in print with tqdm_print
old_print = print


def tqdm_print(*args, **kwargs):
    try:
        tqdm.write(*args, **kwargs)
    except:
        old_print(*args, **kwargs)


inspect.builtins.print = tqdm_print


class LoggerCallback(Callback):

    def __init__(self):
        super().__init__()

    def on_epoch_end(self, epoch, logs={}):
        metrics = self.params['metrics']
        metric_format = '{name}: {value:0.5f}'
        strings = [metric_format.format(
            name=metric,
            value=np.mean(logs[metric], axis=None)
        ) for metric in metrics if metric in logs]
        epoch_output = 'Epoch {value:05d}: '.format(value=(epoch + 1))
        output = epoch_output + ', '.join(strings)
        print(output)


class CycleReduceLROnPlateau(ReduceLROnPlateau):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_lr = None
        self.min_lr_counter = 0

    def on_train_begin(self, logs=None):
        super().on_train_begin(logs)
        self.start_lr = K.get_value(self.model.optimizer.lr)

    def on_epoch_end(self, epoch, logs=None):
        super().on_epoch_end(epoch, logs)
        new_lr = K.get_value(self.model.optimizer.lr)
        if new_lr == self.min_lr:
            self.min_lr_counter += 1
        if self.min_lr_counter >= 1.5 * self.patience:
            K.set_value(self.model.optimizer.lr, self.start_lr)
            if self.verbose > 0:
                print('\nEpoch %05d: returning to start learning rate %s.' % (epoch + 1, self.start_lr))
            self.cooldown_counter = self.cooldown
            self.wait = 0


class ImageStorage:

    def __init__(self):
        self.images = []
        self.labels = []
        self.files = []

    def load_train_images(self):
        files = [os.path.relpath(file, TRAIN_DIR) for file in
                 glob(os.path.join(TRAIN_DIR, '*', '*'))]
        with ThreadPool() as p:
            total = len(files)
            with tqdm(desc='Loading train files', total=total) as pbar:
                for result in p.imap_unordered(self._load_train_image, files):
                    image, label = result
                    self.images.append(image)
                    self.labels.append(label)
                    pbar.update()

    def load_test_images(self):
        files = [os.path.relpath(file, TEST_DIR) for file in
                 glob(os.path.join(TEST_DIR, '*'))]
        with ThreadPool() as p:
            total = len(files)
            with tqdm(desc='Loading test files', total=total) as pbar:
                for result in p.imap_unordered(self._load_test_image, files):
                    image, filename = result
                    self.images.append(image)
                    self.files.append(filename)
                    pbar.update()

    def shuffle_train_data(self):
        assert len(self.images) == len(self.labels)
        data = list(zip(self.images, self.labels))
        np.random.shuffle(data)
        self.images, self.labels = zip(*data)
        self.images = list(self.images)
        self.labels = list(self.labels)

    @staticmethod
    def _load_train_image(file):
        label = os.path.dirname(file)
        filename = os.path.basename(file)
        image = cv2.imread(os.path.join(TRAIN_DIR, label, filename))
        return image, label

    @staticmethod
    def _load_test_image(file):
        filename = os.path.basename(file)
        image = cv2.imread(os.path.join(TEST_DIR, filename))
        return image, filename


class ImageSequence(Sequence):

    def __init__(self, data, params):
        self.data = data
        self.len_ = len(self.data.images)
        self.batch_size = params['batch_size']
        self.augment = params['augment']

    def __len__(self):
        return np.ceil(self.len_ / self.batch_size).astype('int')

    @abstractmethod
    def __getitem__(self, item):
        raise NotImplementedError

    @staticmethod
    @jit(nopython=True, nogil=True)
    def _crop_image(args):
        image, side_len, center = args
        h, w, _ = image.shape
        if center is False:
            h_start = np.random.randint(0, h - side_len)
            w_start = np.random.randint(0, w - side_len)
        else:
            h_start = np.floor_divide(h - side_len, 2)
            w_start = np.floor_divide(w - side_len, 2)
        return image[h_start:h_start + side_len, w_start:w_start + side_len].copy()

    @staticmethod
    @jit(nopython=True, nogil=True)
    def _prepare_image(args):
        image, center = args
        if np.random.rand() < 0.3:
            manip = np.random.choice([0, 0, 1, 1, 1, 1, 2, 2])
            if manip == 0:
                rate = np.random.choice([70, 90])
                manip_image = ImageSequence._crop_image((image, CROP_SIDE, center))
                enc_param = [int(cv2.IMWRITE_JPEG_QUALITY), rate]
                _, manip_image = cv2.imencode('.jpg', manip_image, enc_param)
                manip_image = cv2.imdecode(manip_image, 1)
            elif manip == 1:
                scale = np.random.choice([0.5, 0.8, 1.5, 2.0])
                side_len = np.ceil(CROP_SIDE / scale).astype('int')
                manip_image = ImageSequence._crop_image((image, side_len, center))
                manip_image = cv2.resize(manip_image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            else:
                gamma = np.random.choice([0.8, 1.2])
                manip_image = ImageSequence._crop_image((image, CROP_SIDE, center))
                manip_image = adjust_gamma(manip_image, gamma)
            if manip_image.shape != (CROP_SIDE, CROP_SIDE, 3):
                manip_image = ImageSequence._crop_image((manip_image, CROP_SIDE, center))
        else:
            manip_image = ImageSequence._crop_image((image, CROP_SIDE, center))
        return manip_image

    @staticmethod
    @jit(nopython=True, nogil=True)
    def _augment_image(image):
        aug_image = image
        if np.random.rand() < 0.5:
            n_rotate = np.random.choice([1, 2, 3])
            for _ in range(n_rotate):
                aug_image = np.rot90(aug_image)
        if np.random.rand() < 0.5:
            k_size = np.random.choice([3, 5])
            aug_image = cv2.GaussianBlur(aug_image, (k_size, k_size), 0)
        return aug_image


class TrainSequence(ImageSequence):

    def __init__(self, data, params):
        super().__init__(data, params)
        self.balance = params['balance']
        # shuffle before start
        self.on_epoch_end()

    def __getitem__(self, idx):
        x = self.data.images[idx * self.batch_size:(idx + 1) * self.batch_size]
        y = self.data.labels[idx * self.batch_size:(idx + 1) * self.batch_size]
        label_ids = [LABEL2ID[label] for label in y]
        images_batch = []
        with ThreadPool() as p:
            args = list(zip(x, [False] * len(x)))
            for image in p.imap(self._prepare_image, args):
                images_batch.append(image)
            if self.augment != 0:
                augmented_batch = []
                for image in p.imap(self._augment_image, images_batch):
                    augmented_batch.append(image)
                images_batch = augmented_batch
        labels_batch = []
        for id_ in label_ids:
            ohe = np.zeros(N_CLASS)
            ohe[id_] = 1
            labels_batch.append(ohe)
        images_batch = np.array(images_batch).astype(np.float32)
        labels_batch = np.array(labels_batch)
        if self.balance == 0:
            return images_batch, labels_batch
        else:
            weights = compute_sample_weight('balanced', label_ids)
            return images_batch, labels_batch, weights

    def on_epoch_end(self):
        self.data.shuffle_train_data()


class ValSequence(ImageSequence):

    def __init__(self, data, params):
        super().__init__(data, params)
        self.balance = params['balance']

    def __getitem__(self, idx):
        x = self.data.images[idx * self.batch_size:(idx + 1) * self.batch_size]
        y = self.data.labels[idx * self.batch_size:(idx + 1) * self.batch_size]
        label_ids = [LABEL2ID[label] for label in y]
        images_batch = []
        with ThreadPool() as p:
            args = list(zip(x, [True] * len(x)))
            for image in p.imap(self._prepare_image, args):
                images_batch.append(image)
            if self.augment != 0:
                augmented_batch = []
                for image in p.imap(self._augment_image, images_batch):
                    augmented_batch.append(image)
                images_batch = augmented_batch
        labels_batch = []
        for id_ in label_ids:
            ohe = np.zeros(N_CLASS)
            ohe[id_] = 1
            labels_batch.append(ohe)
        images_batch = np.array(images_batch).astype(np.float32)
        labels_batch = np.array(labels_batch)
        if self.balance == 0:
            return images_batch, labels_batch
        else:
            weights = compute_sample_weight('balanced', label_ids)
            return images_batch, labels_batch, weights


class TestSequence(ImageSequence):

    def __init__(self, data, params):
        super().__init__(data, params)

    def __getitem__(self, idx):
        x = self.data.images[idx * self.batch_size:(idx + 1) * self.batch_size]
        # for TTA
        if self.augment == 0:
            images_batch = x
        else:
            images_batch = []
            with ThreadPool() as p:
                for image in p.imap(self._augment_image, x):
                    images_batch.append(image)
        images_batch = np.array(images_batch).astype(np.float32)
        return images_batch

