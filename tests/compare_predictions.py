"""
Compares two prediction matfiles
"""
import numpy as np
import scipy.io as sio
import sys

m1 = sio.loadmat(sys.argv[1])
m2 = sio.loadmat(sys.argv[2])
th = float(sys.argv[3])

if 'com' in m1.keys():
    print("Checking for parity between predictions...")
    assert np.mean(np.abs(m1['com']-m2['com'])) < th
    print("Good!")
elif 'pred' in m2.keys():
    print("Checking for parity between predictions...")
    assert np.mean(np.abs(m1['pred']-m2['pred'])) < th
    print("Good!")
else:
    raise Exception("Expected fields (pred, com) not found in inputs")