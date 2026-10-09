"""Prepare original-file handoff to an already logged-in Windows messenger."""
import ctypes
import os
import struct
import subprocess
from pathlib import Path


def paste_message(control, text, owner_handle):
    """Paste multiline text without interpreting newlines as Send key presses."""
    import win32clipboard
    win32clipboard.OpenClipboard(owner_handle)
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
    control.type_keys('^v', set_foreground=False)
from media import fingerprint


def wechat_attachment_paths(paths):
    from media import PHOTOS, RAW
    result=[Path(path).resolve(strict=True) for path in paths]
    if len(result)==1 and result[0].suffix.lower()=='.zip':return result
    if not 1<=len(result)<=8 or any(path.suffix.lower() not in PHOTOS | RAW for path in result):
        raise ValueError('微信直接发送限 1–8 张图片；9 张及以上或含视频请打包 ZIP')
    return result


def selected_files(project, submission):
    items = {i['id']: i for i in project.snapshot()['items']}
    result = []
    for original in submission['files']:
        item = items.get(original['id'], original)
        path = Path(item['path'])
        if fingerprint(path) != {'size': original['size'], 'mtime': original['mtime']}:
            raise ValueError('原片已改变，请重新选片：' + original['name'])
        result.append(path.resolve())
    return result


def copy_files(paths, owner):
    """CF_HDROP: paste as original file attachments, not recompressed images."""
    from ctypes import wintypes
    paths = [Path(p).resolve(strict=True) for p in paths]
    payload = struct.pack('<IiiII', 20, 0, 0, 0, 1) + ('\0'.join(str(p) for p in paths) + '\0\0').encode('utf-16-le')
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user.SetClipboardData.restype = wintypes.HANDLE
    handle = kernel.GlobalAlloc(0x42, len(payload))
    if not handle:
        raise OSError('无法分配剪贴板内存')
    transferred = False
    try:
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise OSError('无法写入剪贴板')
        ctypes.memmove(pointer, payload, len(payload))
        kernel.GlobalUnlock(handle)
        if not user.OpenClipboard(owner):
            raise OSError('剪贴板正被其他程序占用，请重试')
        try:
            if not user.EmptyClipboard() or not user.SetClipboardData(15, handle):
                raise OSError('复制文件失败')
            transferred = True
        finally:
            user.CloseClipboard()
    finally:
        if not transferred:
            kernel.GlobalFree(handle)


def open_client(channel):
    candidates = {
        'qq': [r'D:\Program Files\Tencent\QQNT\QQ.exe'],
        'wechat': [r'D:\Program Files\Tencent\Weixin\Weixin.exe'],
    }[channel]
    for base in (os.environ.get('ProgramFiles', ''), os.environ.get('ProgramFiles(x86)', ''), os.environ.get('LOCALAPPDATA', '')):
        for relative in ({'qq': ('Tencent/QQNT/QQ.exe', 'Tencent/QQ/Bin/QQ.exe'),
                          'wechat': ('Tencent/Weixin/Weixin.exe', 'Tencent/WeChat/WeChat.exe')}[channel]):
            candidates.append(str(Path(base) / relative))
    exe = next((p for p in candidates if Path(p).is_file()), None)
    if not exe:
        raise ValueError('没有找到客户端，请先手动打开 QQ 或微信')
    subprocess.Popen([exe])
