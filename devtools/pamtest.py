#!/usr/bin/env python3
# Run a PAM service's auth stack the way a greeter would: every password
# prompt is answered with PASSWORD, info/error messages are printed.
# Usage: pamtest.py SERVICE [ask|empty|wrong]
import ctypes, ctypes.util, getpass, sys, time

libpam = ctypes.CDLL(ctypes.util.find_library("pam"))
libc = ctypes.CDLL(ctypes.util.find_library("c"))
libc.calloc.restype = ctypes.c_void_p
libc.strdup.restype = ctypes.c_void_p
libc.strdup.argtypes = [ctypes.c_char_p]

class Msg(ctypes.Structure):
    _fields_ = [("style", ctypes.c_int), ("msg", ctypes.c_char_p)]

class Resp(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_void_p), ("retcode", ctypes.c_int)]

CONV = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int,
                        ctypes.POINTER(ctypes.POINTER(Msg)),
                        ctypes.POINTER(ctypes.POINTER(Resp)), ctypes.c_void_p)

class Conv(ctypes.Structure):
    _fields_ = [("conv", CONV), ("appdata", ctypes.c_void_p)]

service, mode = sys.argv[1], sys.argv[2]
password = {"ask": None, "empty": "", "wrong": "definitely-not-it"}[mode]
if password is None:
    password = getpass.getpass("password: ")
t0 = time.monotonic()

def ts():
    return f"[{time.monotonic() - t0:6.2f}s]"

@CONV
def conv(n, msgs, resp, _):
    arr = libc.calloc(n, ctypes.sizeof(Resp))
    r = ctypes.cast(arr, ctypes.POINTER(Resp))
    for i in range(n):
        m = msgs[i].contents
        text = m.msg.decode(errors="replace") if m.msg else ""
        if m.style in (1, 2):  # PAM_PROMPT_ECHO_OFF / ON
            print(ts(), "prompt:", text, "-> answering", mode, flush=True)
            r[i].resp = libc.strdup(password.encode())
        else:
            print(ts(), "message:", text, flush=True)
    resp[0] = r
    return 0

handle = ctypes.c_void_p()
c = Conv(conv, None)
user = getpass.getuser().encode()
rc = libpam.pam_start(service.encode(), user, ctypes.byref(c), ctypes.byref(handle))
assert rc == 0, rc
rc = libpam.pam_authenticate(handle, 0)
libpam.pam_strerror.restype = ctypes.c_char_p
print(ts(), "pam_authenticate:", rc, libpam.pam_strerror(handle, rc).decode(), flush=True)
libpam.pam_end(handle, rc)
