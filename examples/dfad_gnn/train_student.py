import os
# os.environ['CUDA_VISIBLE_DEVICES'] = '0'
# os.environ['TL_BACKEND'] = 'torch'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2' 
# 0:Output all; 1:Filter out INFO; 2:Filter out INFO and WARNING; 3:Filter out INFO, WARNING, and ERROR

import argparse
import tensorlayerx as tlx
from gammagl.models import GINModel, DFADModel, DFADGenerator
from gammagl.loader import DataLoader
from gammagl.data import Graph
from gammagl.datasets import TUDataset
from tensorlayerx.model import TrainOneStep, WithLoss
import numpy 
import scipy.sparse as sp

class GeneratorLoss(WithLoss):
    def __init__(self, net, loss_fn, student, teacher):
        super(GeneratorLoss, self).__init__(backbone=net, loss_fn=loss_fn)
        self.student = student
        self.teacher = teacher

    def forward(self, z, label_no_use):
        generated_graph = self.backbone_network(z)
        adj, nodes_logits = generated_graph
        loader = Data_construct(z.shape[0], adj, nodes_logits)
        for data in loader:
            x, edge_index, num_nodes, batch = data.x, data.edge_index, data.num_nodes, data.batch
            student_logits = self.student(x, edge_index, num_nodes, batch)
            teacher_logits = self.teacher(x, edge_index, batch)
            student_logits = tlx.nn.Softmax()(student_logits)
            teacher_logits = tlx.nn.Softmax()(teacher_logits)
        loss = -self._loss_fn(student_logits, teacher_logits)
        return loss

class StudentLoss(WithLoss):
    def __init__(self, net, loss_fn, batch_size):
        super(StudentLoss, self).__init__(backbone=net, loss_fn=loss_fn)
        self.loss_fn = loss_fn
        self.batch_size = batch_size

    def forward(self, data, label):
        logits = self.backbone_network(data['x'], data['edge_index'], data['x'].shape[0], data['batch'])
        loss = self._loss_fn(logits, label)
        return loss

def dense_to_sparse(adj_mat):
    adj_mat = tlx.convert_to_numpy(adj_mat)
    adj_mat = sp.coo_matrix(adj_mat)
    row = tlx.convert_to_tensor(adj_mat.row, dtype=tlx.int64)
    col = tlx.convert_to_tensor(adj_mat.col, dtype=tlx.int64)
    res = tlx.stack((row, col))
    return res

def Data_construct(batch_size, edges_logits, nodes_logits):
    nodes_logits = tlx.softmax(nodes_logits, -1)
    max_indices = tlx.argmax(nodes_logits, axis=2, keepdim=True)
    feature = tlx.zeros_like(nodes_logits)
    slices = len(feature)
    for i in range(len(max_indices)):  
        for j in range(len(max_indices[0])):  
            index = max_indices[i, j]
            if index < 0 or index >= slices:
                raise IndexError("Index out of bounds")
            feature[index, i, j] = 1
    # feature.scatter_(2, max_indices, 1)
    edges_logits = tlx.sigmoid(tlx.cast(edges_logits, dtype=tlx.float32))
    edges_logits = (edges_logits>0.3).long()
    data_list = []
    s=len(feature)
    for i in range(s):
        edge = dense_to_sparse(edges_logits[i])
        x = feature[i]
        data = Graph(x=x, edge_index=edge)
        # draw(args,data,'filename',i)
        data_list.append(data)
    G_data = DataLoader(data_list, batch_size=batch_size, shuffle=False)
    return G_data
    

def generate_graph(args, generator):
    z = tlx.random_normal((args.batch_size, args.nz))  # 随机噪声
    adj, nodes_logits = generator(z) # z通过生成器，生成一个图
    loader = Data_construct(args.batch_size, adj, nodes_logits)
    return loader


def train_student(args):
    dataset = TUDataset(args.dataset_path,args.dataset)
    dataloader = DataLoader(dataset, batch_size=args.batch_size)
    
    # dataset_unit = len(dataset) // 10
    # train_set = dataset[4 * dataset_unit:]
    # val_set = dataset[:2 * dataset_unit]
    # test_set = dataset[2 * dataset_unit: 4 * dataset_unit]
    
    # train_loader = DataLoader(train_set, batch_size=args.batch_size)
    # val_loader = DataLoader(val_set, batch_size=args.batch_size)
    # test_loader = DataLoader(test_set, batch_size=args.batch_size)
    
    teacher = GINModel(
        in_channels=max(dataset.num_features, 1),
        hidden_channels=128,
        out_channels=dataset.num_classes,
        num_layers=5,
        name="GIN"
    )
    
    student = DFADModel(
        model_name=args.student,
        feature_dim=max(dataset.num_features, 1),
        hidden_dim=args.hidden_units,
        num_classes=dataset.num_classes,
        num_layers=args.num_layers,
        drop_rate=args.student_dropout
    )
    
    
    #initialize generator
    x_example = dataset[0].x
    generator = DFADGenerator([64, 128, 256], args.nz, x_example.shape[0], x_example.shape[1], args.generator_dropout)
    generator(tlx.random_normal((args.batch_size, args.nz)))

    optimizer_s = tlx.optimizers.Adam(lr=args.student_lr, weight_decay=args.student_l2_coef)
    optimizer_g = tlx.optimizers.Adam(lr=args.generator_lr, weight_decay=args.generator_l2_coef)
    
    teacher.load_weights("./teacher_" + args.dataset + ".npz", format='npz_dict', skip=True)
    teacher.set_eval()

    student_trainable_weight = student.trainable_weights
    # print("length of student trainable weights:", len(student_trainable_weight))
    generator_trainable_weights = generator.trainable_weights
    s_loss_fun = tlx.losses.absolute_difference_error
    g_loss_fun = tlx.losses.softmax_cross_entropy_with_logits

    s_with_loss = StudentLoss(student, s_loss_fun, args.batch_size)

    g_with_loss = GeneratorLoss(generator, g_loss_fun, student, teacher) # generator的损失函数
    s_train_one_step = TrainOneStep(s_with_loss, optimizer_s, student_trainable_weight)
    g_train_one_step = TrainOneStep(g_with_loss, optimizer_g, generator_trainable_weights)

    epochs = args.n_epoch
    student_epochs = args.student_epochs

    best_acc = 0
    for epoch in range(epochs):
        student.set_train()
        for _ in range(student_epochs):
            # train student model
            loader = generate_graph(args, generator)
            teacher.set_eval()
            for data in loader:
                t_logits = teacher(data.x, data.edge_index, data.batch)
                s_loss = s_train_one_step(data, t_logits)
        student.set_eval()

        # train generator
        generator.set_train()
        z = tlx.random_normal((args.batch_size, args.nz))  # 随机噪声
        g_loss = g_train_one_step(z, None)
        generator.set_eval()

        total_correct = 0
        for data in dataloader:
            test_logits = student(data.x, data.edge_index, data.x.shape[0], data.batch)
            teacher_logits = teacher(data.x, data.edge_index, data.batch)
            pred = tlx.argmax(test_logits, axis=-1)
            total_correct += int((numpy.sum(tlx.convert_to_numpy(pred == data['y']).astype(int))))
        test_acc = total_correct / len(dataset)

        if test_acc > best_acc:
            best_acc = test_acc
            student.save_weights("./{0}.npz".format(args.dataset), format="npz_dict")
        print("Epoch [{:0>3d}]  ".format(epoch + 1)
              + "   acc: {:.4f}".format(test_acc))
    
    for data in dataloader:
        teacher_logits = teacher(data.x, data.edge_index, data.batch)
        pred = tlx.argmax(teacher_logits, axis=-1)
        total_correct += int((numpy.sum(tlx.convert_to_numpy(pred == data['y']).astype(int))))
    teacher_acc = total_correct / len(dataset)
    
    print('teacher_acc:', teacher_acc)
    print('student_acc:', best_acc)
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--student", type=str, default='gin', help="student model")
    parser.add_argument("--student_lr", type=float, default=0.0005, help="learning rate of student model")
    parser.add_argument("--generator_lr", type=float, default=0.0005, help="learning rate of generator")
    parser.add_argument("--n_epoch", type=int, default=3000, help="number of epoch")
    parser.add_argument("--student_epochs", type=int, default=5)
    parser.add_argument("--num_layers", type=int, default=5)
    parser.add_argument("--hidden_units", type=int, default=128, help="dimention of hidden layers")
    parser.add_argument("--student_l2_coef", type=float, default=5e-4, help="l2 loss coeficient for student")
    parser.add_argument("--generator_l2_coef", type=float, default=5e-4, help="l2 loss coeficient for generator")
    parser.add_argument('--dataset', type=str, default='MUTAG', help='dataset(MUTAG/IMDB-BINARY/REDDIT-BINARY)')
    parser.add_argument("--dataset_path", type=str, default=r'', help="path to save dataset")
    parser.add_argument("--generator_dropout", type=float, default=0.5)
    parser.add_argument("--student_dropout", type=float, default=0.5)
    parser.add_argument("--batch_size", type=int, default=128)
    parser.add_argument("--nz", type=int, default=32)
    parser.add_argument("--gpu", type=int, default=0)
    args = parser.parse_args()
    
    if args.gpu >= 0:
        tlx.set_device("GPU", args.gpu)
    else:
        tlx.set_device("CPU")
    
    train_student(args)