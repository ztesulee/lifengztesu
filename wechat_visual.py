"""Windows OCR fallback for Weixin clients without UIA controls."""
import ctypes
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from PIL import ImageGrab
from delivery import copy_files
from debug_log import logger
from qq_sender import wait_for


def recognize_image(path):
    source = Path(__file__).with_name('windows_ocr.ps1').read_text(encoding='utf-8')
    source = source.replace('param([string]$ImagePath)', "$ImagePath = '" + str(path).replace("'", "''") + "'", 1)
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', source],
        capture_output=True, encoding='utf-8', errors='replace', timeout=20, creationflags=0x08000000)
    if result.returncode:
        raise ValueError('Windows OCR 无法识别微信界面：' + result.stderr[-500:])
    return json.loads(result.stdout.lstrip('\ufeff'))


def line_box(line):
    words = line['words']
    return (min(w['x'] for w in words), min(w['y'] for w in words),
            max(w['x']+w['w'] for w in words), max(w['y']+w['h'] for w in words))


def compact(text):
    return re.sub(r'\s+', '', text)


def archive_label_matches(text, filename):
    value, expected = compact(text), compact(filename)
    if expected in value:
        return True
    pieces = re.split(r'\.{2,}|…+', value)
    return (len(pieces) == 2 and len(pieces[0]) >= 4 and len(pieces[1]) >= 5
            and expected.startswith(pieces[0]) and expected.endswith(pieces[1]))


def green_send_box(image):
    import numpy as np
    import cv2
    pixels = np.asarray(image.convert('RGB'))
    red, green, blue = [pixels[:,:,i].astype('int16') for i in range(3)]
    mask = ((green > 100) & (green > red+40) & (red < 120) & (blue < 190)).astype('uint8')*255
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    matches = [(int(x),int(y),int(x+w),int(y+h)) for x,y,w,h,area in stats[1:]
               if 25 <= w <= 140 and 15 <= h <= 65 and area > w*h*.45]
    return matches[0] if len(matches) == 1 else None


def zip_icon_boxes(image):
    import cv2
    import numpy as np
    pixels = np.asarray(image.convert('RGB')).astype('int16')
    red, green, blue = [pixels[:,:,i] for i in range(3)]
    mask = ((red > 180) & (green > 90) & (green < 200) & (blue < 100)
            & (red > green+35)).astype('uint8')*255
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    return [(int(x),int(y),int(x+w),int(y+h)) for x,y,w,h,area in stats[1:]
            if 15 <= w <= 65 and 25 <= h <= 85 and area > w*h*.4]


def find_file_icon(image, template):
    """Match the supplied outline at multiple DPI scales; reject ambiguity."""
    import cv2
    import numpy as np
    def edges(value):
        return cv2.Canny(np.asarray(value.convert('L')), 50, 120)
    reference = edges(template)
    ys,xs = np.nonzero(reference)
    if not len(xs):
        raise ValueError('微信文件图标模板为空')
    reference = np.asarray(template.convert('L'))[max(0,ys.min()-3):ys.max()+4,max(0,xs.min()-3):xs.max()+4]
    full = edges(image)
    left,top = int(image.width*.25),int(image.height*.55)
    region = full[top:,left:]
    candidates = []
    for scale in sorted(set(list(np.linspace(.7,2.5,25)) + [1,1.25,1.5,1.75,2,2.25,2.5])):
        w,h = max(8,round(reference.shape[1]*scale)),max(8,round(reference.shape[0]*scale))
        if h > region.shape[0] or w > region.shape[1]:
            continue
        resized = cv2.Canny(cv2.resize(reference,(w,h),interpolation=cv2.INTER_CUBIC),50,120)
        scores = cv2.matchTemplate(region,resized,cv2.TM_CCORR_NORMED)
        _,score,_,point = cv2.minMaxLoc(scores)
        # Find separate possible icons, not just the best match.
        while score >= .78 and len(candidates) < 100:
            x,y = point
            candidates.append((score,left+x+w//2,top+y+h//2,w,h))
            scores[max(0,y-h//2):y+h//2+1,max(0,x-w//2):x+w//2+1] = 0
            _,score,_,point = cv2.minMaxLoc(scores)
    unique = []
    for candidate in sorted(candidates,reverse=True):
        if not any(abs(candidate[1]-old[1]) < max(candidate[3],old[3])*.6
                   and abs(candidate[2]-old[2]) < max(candidate[4],old[4])*.6 for old in unique):
            unique.append(candidate)
    if len(unique) != 1:
        return None
    return unique[0][1],unique[0][2]


def search_lines(lines, width, height, text):
    """Only accept search text in the main panel's upper-left search area."""
    return [line for line in lines if compact(line['text']) in text
            and line_box(line)[1] < height/4 and line_box(line)[0] < width/2]


def chat_title_text(lines, width, height):
    candidates = []
    for line in lines:
        value = compact(line['text'])
        x1,y1,x2,y2 = line_box(line)
        if (width*.25 < x1 < width*.75 and 28 < y1 < min(140,height*.15)
                and value not in ('搜索','发送','0','O','o')
                and re.search(r'[\w\u4e00-\u9fff]',value)):
            candidates.append((y1,x1,x2,y2,value))
    if not candidates:return None
    candidates.sort()
    first = candidates[0]
    aligned = sorted([item for item in candidates if abs(item[0]-first[0]) <= 8],key=lambda item:item[1])
    name = aligned[0][4]
    right = aligned[0][2]
    for item in aligned[1:]:
        if item[1]-right > 25:break
        name += item[4]
        right = item[2]
    return name


def toolbar_point(width, height, action):
    """Relative positions for the Weixin toolbar shown in the current client."""
    if width < 650 or height < 450:
        raise ValueError('微信窗口过小，请展开主窗口后再发送')
    if action == 'file':
        return int(width*.47), height-34
    return int(width*.65), height-85


def choose_archive_file(dialog, path):
    from qq_sender import controls_by_id, keyboard_activate
    edits = [edit for edit in controls_by_id(dialog, '1148', 'Edit')
             if edit.is_visible() and edit.is_enabled()]
    logger.debug('WECHAT file dialog filename_candidates=%s controls=%s', len(edits),
                 [(control.element_info.control_type, control.element_info.automation_id,
                   control.window_text()) for control in dialog.descendants()
                  if control.element_info.control_type in ('Edit', 'Button', 'SplitButton')])
    if len(edits) != 1:
        raise ValueError('微信文件选择窗口无法唯一定位文件名输入框，请查看 WECHAT file dialog 调试信息')
    editor = edits[0]
    editor.set_edit_text(str(path))
    wait_for(lambda: editor.get_value().strip().strip('"') == str(path),
             '微信文件选择窗口未收到完整 ZIP 路径', timeout=3)
    # Enter from File name invokes Open without confusing the split-button arrow.
    keyboard_activate(editor)


class WeChatVisualSender:
    def __init__(self, window):
        self.window = window
        self.pid = window.process_id()

    def snapshot(self, header=False):
        self.window.set_focus()
        time.sleep(.25)
        foreground = ctypes.windll.user32.GetForegroundWindow()
        if foreground != self.window.handle:
            import win32gui, win32process
            if (win32process.GetWindowThreadProcessId(foreground)[1] != self.pid
                    or win32gui.GetAncestor(foreground, 3) != self.window.handle):
                raise ValueError('微信目标窗口不在前台，未继续输入')
        rect = self.window.rectangle()
        with tempfile.TemporaryDirectory(prefix='return-wechat-ocr-') as folder:
            path = Path(folder) / 'window.png'
            screenshot = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
            if header:
                from PIL import ImageOps, Image
                left,top=int(rect.width()*.25),28
                right,bottom=int(rect.width()*.8),100
                cropped=screenshot.crop((left,top,right,bottom))
                cropped=ImageOps.autocontrast(cropped.convert('L'))
                cropped.resize((cropped.width*3,cropped.height*3),Image.Resampling.LANCZOS).save(path)
                lines=recognize_image(path)
                for line in lines:
                    for word in line['words']:
                        word['x']=word['x']/3+left;word['y']=word['y']/3+top
                        word['w']/=3;word['h']/=3
                return lines
            screenshot.save(path)
            return recognize_image(path)

    def read_chat_title(self):
        rect=self.window.rectangle()
        lines=self.snapshot(header=True)
        title=chat_title_text(lines,rect.width(),rect.height())
        logger.debug('WECHAT dedicated header title=%s OCR=%s',title,
                     [(line['text'],line_box(line)) for line in lines])
        return title

    def paste_text(self, text):
        import win32clipboard
        win32clipboard.OpenClipboard(self.window.handle)
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        self.window.type_keys('^v', set_foreground=False)

    def click_line(self, line):
        x1,y1,x2,y2 = line_box(line)
        self.window.click_input(coords=(int((x1+x2)/2), int((y1+y2)/2)))

    def connect(self, account):
        self.account = account
        self.window.set_focus()
        self.window.type_keys('^f', set_foreground=False)
        lines = self.snapshot()
        rect = self.window.rectangle()
        search = search_lines(lines, rect.width(), rect.height(), {'搜索', '搜索联系人'})
        logger.debug('WECHAT OCR search candidates=%s upper_left=%s', len(search),
                     [(line['text'], line_box(line)) for line in lines
                      if line_box(line)[1] < rect.height()/4 and line_box(line)[0] < rect.width()/2])
        if len(search) == 1:
            self.click_line(search[0])
        elif len(search) > 1:
            raise ValueError('微信 OCR 搜索框候选不唯一，请查看 WECHAT OCR search 调试信息')
        # Ctrl+F also focuses a search field already containing the previous ID.
        self.window.type_keys('^a', set_foreground=False)
        self.paste_text(account)
        time.sleep(.8)
        self.window.type_keys('{ENTER}', set_foreground=False)
        time.sleep(.6)
        def chat_title():
            lines = self.snapshot()
            title = chat_title_text(lines,rect.width(),rect.height())
            logger.debug('WECHAT chat title detected=%s header=%s window=%s',title,
                         [(line['text'],line_box(line)) for line in lines if line_box(line)[1]<140],rect)
            return title or self.read_chat_title()
        self.nickname = wait_for(chat_title, '微信 Enter 后未确认聊天标题，请检查搜索结果', timeout=10)
        wait_for(self.check_target, '微信目标聊天尚未就绪', timeout=5)
        logger.info('WECHAT Enter search opened account=%s chat_title=%s', account, self.nickname)

    def check_target(self):
        rect = self.window.rectangle()
        title=chat_title_text(self.snapshot(),rect.width(),rect.height())
        return (title or self.read_chat_title()) == self.nickname

    def input_area(self):
        if not self.check_target():
            raise ValueError('微信目标聊天标题变化，未输入')
        lines = self.snapshot()
        sends = [l for l in lines if compact(l['text']) == '发送'
                 and line_box(l)[1] > self.window.rectangle().height()/2]
        rect = self.window.rectangle()
        if len(sends) == 1:
            _, top, _, _ = line_box(sends[0])
            point = (int(rect.width()*.6), int(top-60))
        else:
            point = toolbar_point(rect.width(), rect.height(), 'input')
        logger.debug('WECHAT input point=%s send_candidates=%s', point, len(sends))
        self.window.click_input(coords=point)

    def click_send(self):
        if not self.check_target():
            raise ValueError('微信目标聊天标题变化，未点击发送')
        lines = self.snapshot()
        rect = self.window.rectangle()
        sends = [l for l in lines if compact(l['text']) == '发送'
                 and line_box(l)[0] > rect.width()*.65 and line_box(l)[1] > rect.height()*.85]
        if len(sends) == 1:
            self.click_line(sends[0])
            return
        left, top = int(rect.width()*.65), rect.height()-90
        image = ImageGrab.grab(bbox=(rect.left+left,rect.top+top,rect.right,rect.bottom), all_screens=True)
        box = green_send_box(image)
        logger.debug('WECHAT send OCR_candidates=%s green_button=%s', len(sends), box)
        if box is None:
            raise ValueError('微信右下角未识别到唯一绿色发送按钮，未点击')
        x1,y1,x2,y2 = box
        self.window.click_input(coords=(left+(x1+x2)//2,top+(y1+y2)//2))

    def send_archive_enter(self):
        self.input_area()
        logger.info('WECHAT ZIP Enter send once account=%s chat=%s', self.account, self.nickname)
        self.window.type_keys('{ENTER}', set_foreground=False)

    def zip_icons(self):
        rect = self.window.rectangle()
        image = ImageGrab.grab(bbox=(rect.left,rect.top,rect.right,rect.bottom), all_screens=True)
        return [box for box in zip_icon_boxes(image) if box[0] > rect.width()/3]

    def send_files(self, paths):
        if len(paths) != 1 or Path(paths[0]).suffix.lower() != '.zip':
            raise ValueError('微信只发送一个返图压缩包')
        path = Path(paths[0]).resolve(strict=True)
        if not self.check_target():
            raise ValueError('微信目标聊天标题变化，未选择文件')
        initial = self.snapshot()
        height = self.window.rectangle().height()
        if any('.zip' in compact(l['text']).lower() and line_box(l)[1] > height*.82 for l in initial):
            raise ValueError('微信输入区已有 ZIP 草稿，请先核对并处理，未重复选择文件')
        initial_icons = self.zip_icons()
        if any(box[1] > height*.82 for box in initial_icons):
            raise ValueError('微信输入区已有压缩包草稿，请先核对，未重复发送')
        # The file toolbar icon has no shortcut; click relative to this window.
        rect = self.window.rectangle()
        from PIL import Image
        screenshot = ImageGrab.grab(bbox=(rect.left,rect.top,rect.right,rect.bottom), all_screens=True)
        with Image.open(Path(__file__).parent / 'assets' / 'wechat-file-icon.png') as template:
            point = find_file_icon(screenshot, template)
        logger.debug('WECHAT visual file icon window=%s point=%s', rect, point)
        if point is None:
            raise ValueError('微信发送文件图标未找到或出现多个候选，请展开目标聊天窗口')
        self.window.click_input(coords=point)
        from pywinauto import Desktop
        from qq_sender import controls_by_id
        desktop = Desktop(backend='uia')
        def file_dialog():
            matches = [window for window in desktop.windows(process=self.pid)
                       if window.is_visible() and controls_by_id(window, '1148', 'Edit')]
            return matches[0] if len(matches) == 1 else None
        dialog = wait_for(file_dialog, '点击微信文件按钮后未出现文件选择窗口', timeout=10)
        choose_archive_file(dialog, path)
        wait_for(lambda: file_dialog() is None, '微信文件选择窗口尚未关闭，未向聊天按 Enter', timeout=10)
        time.sleep(.5)
        self.send_archive_enter()
        logger.info('WECHAT ZIP submitted by Enter; upload continues in client; advancing queue')

    def send_message(self, text):
        self.input_area()
        self.paste_text(text)
        time.sleep(.3)
        if not self.check_target():
            raise ValueError('微信留言发送前聊天标题变化，未按 Enter')
        self.window.type_keys('{ENTER}', set_foreground=False)
        logger.info('WECHAT completion message Enter sent once')
        logger.info('WECHAT completion message submitted; advancing queue without waiting for upload')
