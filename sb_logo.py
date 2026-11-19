# -*- coding: utf-8 -*-
"""软件 Logo: 内嵌 base64(32px 窗口图标)。源图 assets/logo.png。"""
import tkinter as tk

DATA = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAEXklEQVR4nM2XXWgcVRTH/+fOnZn9"
    "ymaXfDTEtLWpTQzVVOsHIq2lWgUpFlEQ/MIXsShoKQ0ISkV98akUfVD6KFbtgxWxiC9VEVt9kDZQ"
    "xcaqqbapaD662yS7Ozsz9145sxuppemOZpv6h1mGnbv8zj3nP2fvIdRkAVCtrZ29QsptwhKbicRS"
    "AtkACAuTMTCBMfq0VvoTHYavnzs3PjrHpLmbXK7zMekkdlu23W60htEKBs0RQ0hYICGggmAy9L3t"
    "xeL4XmZHu8vnOx+xE+l3iQiB7yljjGjCzi+UISJtOwnLGIPAKz1aKIy/R7lcbrl0Wo5Z0m7xq2VN"
    "HOpllDFaOW5KqDCYCf2ZQUFWYsiSTjbwvcsOZzGDWcxkthBC3meMNvW0L4qYxUxmSxKix+jIjIsp"
    "YpMzWxDIauR2i2rxCaLoaoaYyexYaS8EAbQxKIUhykrNG0T0qtG/q6RotCDQGi/3rcKhDetw4LZb"
    "sSbbEgXyzyBq937pLEK/xE5beAAWEabDEE8s7cGLg6uxOp3GXd1d2Lv2BmSkhDKmjuXPWuMa2PQ8"
    "OnrXQwWV2EGI+R7wzxmyoa0NQahQUQp+xUNvOo3eVAqe0uDGxd0tqEyj/84h3PzQTiy76WHowItd"
    "CnEpk3AWvpyagi0tJC0LTjKB0VIJo+UyEoIAIeGXpnD1LY9jYNMOTJw8iR+/2AXLSXPDiRWAnO8B"
    "7z4rJd4+PYYu18X9Pd34Y9LDzuMjmA1DZBwX1XIB7b3rsGbLa1DVMoY/3I7pP0fgpPJRSeKIOpYs"
    "538rqDCIUnoxsRcyloWq1lFp0raDIKjAzXRg/ZMfIdvVh6P7h/Dz4bfgptthdHhJKP8XWNLm1xDz"
    "ZuB85W07ykha8nJCqMOoxmsffAP5nj6MfL4Hv3y9B266rSH8Qok4ixjO4l5giBB6M7ju3lfRc/0m"
    "jH13EN9/+hJst6XmnLksskeaFcCcSEhUS1NYeftWrLpjKwpjJ3B0/3Pg8wNJCe35MJ5fO4LMeogj"
    "GWtVvcv55bPo6r8bg5tfgV8q4sgHz6JSPAMnnYcqVWBf04nWpzZCtLeg8tkPmNn3DUhaTQiACCrw"
    "kF1yLW58YDcsN4Xhfc9gcvQQnEwHdOCDkjbyL2yBM9ANPesht+0eqMIsSh8PQ2STgNL/vQREAsov"
    "oX/jDnSsWIHjB3fh12/fgVM3nQkUZHce8qo81MQMTNmPLndwGRtn4RkwRkPYCZw68j5mJ07gp6/e"
    "hJ3MRrVn45EtEf5eQHim8HcGKOWgeuxUQzNSnD5QTwOUX4YOfdjJ1nqzru9OEIwXwF4ZzwPn9wGK"
    "HUC9FBzIRbscf18NojeAHAlT8UEptzmNaE5Rf5+vpAxO2LXnfJ9JNKw/SxiY5p3HGFhvWo3gtQIa"
    "JYzWYySiU1mz5pA4MsxkttA6PEAkiIeGxaIzi5kR+4oPJsVi8TcVVJ82xpDjpqI5kYcEdmqTLy4x"
    "w3k0I2Yy+/8xnF7J8fwvv9OHhnerjM8AAAAASUVORK5CYII="
)


def photo():
    return tk.PhotoImage(data=DATA)
