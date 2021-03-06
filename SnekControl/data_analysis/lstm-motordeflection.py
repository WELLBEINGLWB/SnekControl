from __future__ import print_function, division
import torch
import torch.nn as nn
import matplotlib.pyplot as plt
import numpy as np
import os
import datetime
import logging
import sys


#####################
# Logging and dir
#####################

dir = 'run'+datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
os.mkdir(dir)

logger = logging.getLogger('main')
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('[%(asctime)s] %(message)s', '%H:%M:%S')

handler = logging.FileHandler(dir+'/log.txt')
handler.setFormatter(formatter)
logger.addHandler(handler)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)
logger.addHandler(handler)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

#####################
# Load data
#####################
seq_len = 100
test_size = 0.2
batch_size = 10000

data = None
indices = None


def add_dataset(name):
    global data, indices
    set_data = np.genfromtxt(name, dtype=np.float32, delimiter=',', skip_header=1)
    set_inds = np.arange(seq_len, set_data.shape[0])
    if data is None:
        data = set_data
        indices = set_inds
    else:
        indices = np.append(indices, data.shape[0] + set_inds)
        data = np.concatenate((data, set_data))

    logger.info("loaded %s (%d points)", name, set_data.shape[0])


add_dataset('2018-10-24 14-03.csv')
add_dataset('2018-10-24 15-32.csv')

data_x = data[:, 1:4]
data_y = data[:, 4:7]
baseline_err = data[:, 7:10]
baseline_err -= np.mean(baseline_err, axis=0).reshape(1, -1)
baseline_y = data_y - baseline_err

input_dim = data_x.shape[1]
output_dim = data_y.shape[1]

# test train split and batching
num_datapoints = len(indices)
num_batches = num_datapoints // batch_size
num_train_batches = int((1-test_size) * num_batches)
num_test_batches = num_batches - num_train_batches

np.random.seed(0)
test_batches = np.sort(np.random.choice(range(0, num_batches), num_test_batches, replace=False))
train_batches = np.delete(range(0, num_batches), test_batches)

logger.info("train %d test %d", num_train_batches * batch_size, num_test_batches * batch_size)
logger.info("baseline MSE %.4f", np.mean(np.square(baseline_err)))


def batch_indices(batch):
    return indices[batch*batch_size:(batch+1)*batch_size]


#####################
# Network params
#####################
lstm_dim = 64
lstm_layers = 1
linear_dim = [output_dim]
learning_rate = 5e-3
num_epochs = 10000

#####################
# Define model
#####################


class LSTMModel(nn.Module):
    def __init__(self, input_dim, lstm_dim, lstm_layers, linear_dim):
        super(LSTMModel, self).__init__()

        self.lstm = nn.LSTM(input_dim, lstm_dim, lstm_layers)

        prev_dim = lstm_dim
        self.linear = nn.ModuleList()
        for d in linear_dim:
            self.linear.append(nn.Linear(prev_dim, d))
            prev_dim = d

        logger.info("LSTM %dx%d, Linear %s", lstm_dim, lstm_layers, linear_dim)

    def forward(self, input: torch.Tensor):
        y = input
        if len(y.shape) == 2:
            y = y.unsqueeze(1)

        lstm_out, _ = self.lstm(y)
        y = lstm_out[-1]

        for l in self.linear:
            y = l(y)
        return y


class SigmoidModel(nn.Module):
    def __init__(self, input_dim, linear_dim):
        super(SigmoidModel, self).__init__()

        self.linear = nn.ModuleList()
        for d in linear_dim:
            self.linear.append(nn.Linear(input_dim, d))
            input_dim = d

        logger.info("Sigmoid Linear %s", linear_dim)

    def forward(self, input: torch.Tensor):
        sigmoid = nn.Sigmoid()
        y = input
        for i, l in enumerate(self.linear):
            if i > 0:
                y = sigmoid(y)
            y = l(y)
        return y


if lstm_layers > 0:
    model = LSTMModel(input_dim, lstm_dim, lstm_layers, linear_dim)
else:
    model = SigmoidModel(input_dim*seq_len, linear_dim)

model = model.to(device)

loss_fn = torch.nn.MSELoss()
#optimiser = torch.optim.Adam(model.parameters(), lr=learning_rate / 2)
optimiser = torch.optim.SGD(model.parameters(), lr=learning_rate, momentum=0.9)
scheduler = torch.optim.lr_scheduler.StepLR(optimiser, step_size=500, gamma=0.5)
logger.info("SGD lr=%.2e * %.2f per %d epochs", learning_rate, scheduler.gamma, scheduler.step_size)


# if LSTM (seq_len, batch, input_dim)
# if Linear (batch, seq_len*input_dim)
# linear input is repeated input_dim for each timestep, [[t_0] [t_1] ...]
def make_batch_tensors(batch):
    inds = batch_indices(batch)
    if isinstance(model, LSTMModel):
        X = np.concatenate([data_x[i-seq_len:i, None, :] for i in inds], 1)
    else:
        X = np.stack([data_x[i-seq_len:i, :].flatten() for i in inds])

    X = torch.from_numpy(X).to(device)
    y = torch.from_numpy(data_y[inds, :]).to(device)
    return X, y


def export_model(filename, verbose=False):
    dummy_input = torch.linspace(0, 2, steps=seq_len*output_dim, device=device).reshape(seq_len, output_dim)
    if not isinstance(model, LSTMModel):
        dummy_input = dummy_input.flatten()

    torch.onnx.export(model, dummy_input, filename, verbose=verbose)
    model.eval()
    result = model(dummy_input).cpu().detach().numpy()

    # check the model
    dummy_input = dummy_input.cpu().detach().numpy()
    import onnx
    import caffe2.python.onnx.backend as caffe_backend
    onnx_model = onnx.load(filename)
    onnx.checker.check_model(onnx_model)
    result2 = caffe_backend.run_model(onnx_model, [dummy_input])[0]
    if not np.isclose(result, result2).all():
        raise Exception("model is not consistent: {} {}".format(result, result2))

    onnx_model = onnx.utils.polish_model(onnx_model)
    onnx.save(onnx_model, filename.replace(".onnx", "_opt.onnx"))


def plot_preds_vs_data(y_pred, y_data, y_base, filename):
        plt.figure(figsize=(batch_size / 100, 10 * output_dim))
        y_pred = y_pred.cpu().detach().numpy()
        y_data = y_data.cpu().detach().numpy()
        for p in range(3):
            plt.subplot(output_dim, 1, p + 1)
            plt.plot(y_pred[:, p], label="Preds")
            plt.plot(y_data[:, p], label="Data")
            plt.plot(y_base[:, p], label="Baseline")
            plt.legend()
        plt.savefig(filename, dpi=100)
        plt.close()


#####################
# Train model
#####################

hist_train = []
hist_test_x = []
hist_test_y = []


def plot_loss_hist():
    hist_test_x.append(t)
    hist_test_y.append(loss_test_total)
    plt.semilogy(hist_train[:t], label="Training loss")
    plt.semilogy(hist_test_x, hist_test_y, label="Test loss")
    plt.legend()
    plt.savefig(dir + "/loss.png")
    plt.close()


def dump_preds():
    logger.info("performing full eval")
    dump = np.zeros((num_batches * batch_size, 1 + input_dim + 2 * output_dim))
    for batch in range(0, num_batches):
        inds = batch_indices(batch)
        X, _ = make_batch_tensors(batch)
        y_pred = model(X).cpu().detach().numpy()

        j = batch * batch_size
        dump[j:j+batch_size, 0] = data[inds, 0]
        dump[j:j+batch_size, 1:4] = data_x[inds, :]
        dump[j:j+batch_size, 4:7] = data_y[inds, :]
        dump[j:j+batch_size, 7:10] = y_pred

    logger.info("saving eval.csv")
    np.savetxt(dir+'/eval.csv', dump, fmt='%.4f', delimiter=', ')
    logger.info("complete")


for t in range(num_epochs):
    scheduler.step()
    model.train()
    for batch in train_batches:
        X_train, y_train = make_batch_tensors(batch)

        optimiser.zero_grad()
        y_pred = model(X_train)
        loss = loss_fn(y_pred, y_train)
        loss.backward()
        optimiser.step()

    hist_train.append(loss.item())
    model.eval()
    if t % 10 == 0:
        with torch.no_grad():
            y_preds = []
            y_tests = []
            for batch in test_batches:
                X_test, y_test = make_batch_tensors(batch)
                y_preds.append(model(X_test))
                y_tests.append(y_test)

            y_test = torch.cat(y_tests)
            y_pred = torch.cat(y_preds)

            np.set_printoptions(precision=4)
            loss_test = np.mean(np.square((y_pred - y_test).cpu().detach().numpy()), axis=0)
            loss_test_total = np.mean(loss_test)

            logger.info("Epoch %d MSE Train: %.4f, Test %.4f %s", t, loss.item(), loss_test_total, loss_test)
            export_model(dir+'/model.onnx')

            # update histogram
            plot_loss_hist()

            if t < 100 or t % 100 == 0:
                y_base = baseline_y[batch_indices(test_batches[0])]
                plot_preds_vs_data(y_preds[0], y_tests[0], y_base, dir+"/test%d.png" % t)

        if t % 1000 == 0:
            dump_preds()


