#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALGO BUILDER - অপশন প্রিমিয়াম ব্যাকটেস্ট বিল্ডার (সব টিউনিং সেটিংস থেকে, কোডিং লাগে না)

চালানো (Termux বা যেকোনো Python-এ):
    python algo_builder.py
তারপর ফোনের ব্রাউজারে খুলুন:   http://127.0.0.1:8000

একই ফোল্ডারে থাকতে হবে:
    banknifty_5min.csv       (স্পট ইনডেক্সের ৫ মিনিট ডেটা)
    data_opt/opt_*.csv.gz    (download_options.py যা নামিয়েছে)
অন্য নামে থাকলে:  python algo_builder.py --spot FILE.csv --opt FOLDER --port 8000

লাগে শুধু: numpy, pandas (আপনার Termux-এ আগে থেকেই আছে)
"""
import os
import sys
import json
import uuid
import time
import math
import copy
import glob
import bisect
import argparse
import itertools
import threading
import subprocess
import traceback
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np
import pandas as pd

NAN = float("nan")
OPEN_MIN = 555          # 09:15
BASE_TF = 5             # ডেটার ক্যান্ডেল মিনিট (লোডের সময় নিজে বের হয়)
EPOCH = pd.Timestamp("1970-01-01")

