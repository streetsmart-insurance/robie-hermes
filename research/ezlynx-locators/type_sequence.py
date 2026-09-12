import time
import ctypes

cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")

key_codes = {
    '0': 29, '1': 18, '2': 19, '3': 20, '4': 21,
    '5': 23, '6': 22, '7': 26, '8': 28, '9': 25,
    '/': 44, 'enter': 36, 'tab': 48, 'backspace': 51
}

cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_ushort, ctypes.c_bool]
cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
cg.CGEventPost.argtypes = [ctypes.c_uint, ctypes.c_void_p]

def press_key(key):
    code = key_codes.get(key.lower())
    if code is None:
        return
    event_down = cg.CGEventCreateKeyboardEvent(None, code, True)
    event_up = cg.CGEventCreateKeyboardEvent(None, code, False)
    cg.CGEventPost(0, event_down)
    time.sleep(0.05)
    cg.CGEventPost(0, event_up)
    time.sleep(0.05)

# Press backspace 12 times to clear first date field
for _ in range(12):
    press_key('backspace')

# Type 08/01/2026
for char in "08/01/2026":
    press_key(char)

# Tab to second date field
press_key('tab')

# Press backspace 12 times to clear second date field
for _ in range(12):
    press_key('backspace')

# Type 08/23/2026
for char in "08/23/2026":
    press_key(char)

# Press enter to submit
press_key('enter')
