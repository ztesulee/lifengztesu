"""QQNT UI Automation adapter. Requires an exact local-friend QQ-number match."""
import os
import re
import sys
import time
from pathlib import Path
from debug_log import logger
from app_version import APP_VERSION, QQ_FLOW_VERSION

ROOT = Path(__file__).resolve().parent
_dll_dirs = []
_main_identity = None


def select_main_window(matches, previous=None):
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if len({w.process_id() for w in matches}) > 1:
        raise ValueError('多个 QQ 进程中均发现搜索窗口，无法确定发送账号，请保留需要使用的 QQ 窗口')
    remembered = [w for w in matches if (w.process_id(), getattr(w, 'handle', None)) == previous]
    if len(remembered) == 1:
        logger.debug('MAIN reuse previously confirmed handle=%s', remembered[0].handle)
        return remembered[0]
    titled = [w for w in matches if w.window_text() == 'QQ']
    if len(titled) == 1:
        logger.debug('MAIN select unique QQ main title handle=%s', getattr(titled[0], 'handle', None))
        return titled[0]
    sessions = [w for w in matches if any(e.window_text() == '会话列表' for e in w.descendants())]
    if len(sessions) == 1:
        return sessions[0]
    raise ValueError('同一 QQ 进程存在多个搜索窗口，暂时无法确定主窗口，请展开 QQ 主面板后继续')


def window_minimized(window):
    """QQ chat windows need not expose the UIA Window pattern."""
    handle = getattr(window, 'handle', None)
    if isinstance(handle, int) and handle:
        import ctypes
        return bool(ctypes.windll.user32.IsIconic(handle))
    try:
        return window.is_minimized()
    except Exception as exc:
        if type(exc).__name__ == 'NoPatternInterfaceError':
            return False
        raise


def select_chat_window(matches, foreground_handle):
    if not matches:
        return None
    active = [w for w in matches if getattr(w, 'handle', None) == foreground_handle]
    if len(active) == 1:
        return active[0]
    if len(matches) == 1:
        return matches[0]
    raise ValueError('存在多个同名聊天区域，未能确认搜索后激活的目标窗口，已暂停发送')


def recover_main_window(desktop, identity):
    if identity is None:
        return None
    import win32gui, win32process
    pid, handle = identity
    if (not win32gui.IsWindow(handle)
            or win32process.GetWindowThreadProcessId(handle)[1] != pid
            or Path(process_executable(pid) or '').name.casefold() != 'qq.exe'):
        logger.debug('MAIN cached handle no longer valid: %s', identity)
        return None
    logger.info('MAIN recover verified handle=%s pid=%s rect=%s visible=%s iconic=%s',
                handle, pid, win32gui.GetWindowRect(handle), win32gui.IsWindowVisible(handle), win32gui.IsIconic(handle))
    if win32gui.IsIconic(handle):
        win32gui.ShowWindow(handle, 9)  # SW_RESTORE; keep original monitor/size.
    elif not win32gui.IsWindowVisible(handle):
        win32gui.ShowWindow(handle, 5)  # SW_SHOW; never toggle a visible window.
    window = desktop.window(handle=handle).wrapper_object()
    window.set_focus()
    window.type_keys('^f', set_foreground=False)
    return window


def find_chat_input(window):
    names = ('输入', '输入消息', '消息输入框', '消息输入')
    controls = window.descendants()
    toolbar = window.descendants(title='文件弹出菜单', control_type='Button')
    floor = toolbar[0].rectangle().bottom if len(toolbar) == 1 else None
    bounds = window.rectangle()
    candidates = []
    named = []
    for control in controls:
        info = control.element_info
        if info.control_type not in ('Edit', 'Document') or not control.is_visible() or not control.is_enabled():
            continue
        name = control.window_text()
        rect = control.rectangle()
        logger.debug('MESSAGE input candidate type=%s id=%s known_name=%s text_length=%s rect=%s',
                     info.control_type, info.automation_id, name in names, len(name), rect)
        if name in ('搜索', '消息列表'):
            continue
        if name in names:
            named.append(control)
        # QQNT may expose an unnamed rich-text Document. Its editor is below
        # the file toolbar; search boxes and the message list are above it.
        if (floor is not None and rect.top >= floor and rect.bottom <= bounds.bottom
                and rect.left >= bounds.left and rect.right <= bounds.right
                and rect.width() > 0 and rect.height() > 0):
            candidates.append(control)
    if len(named) == 1:
        return named[0]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError('无法唯一定位聊天输入框，请查看 MESSAGE input candidate 调试信息')


def message_input_point(window):
    toolbar = window.descendants(title='文件弹出菜单', control_type='Button')
    # Empty QQ editors disable Send. Its geometry is still a valid boundary.
    sends = [e for e in window.descendants(title='发送')
             if e.element_info.control_type in ('Button', 'SplitButton') and e.is_visible()]
    logger.debug('MESSAGE boundaries toolbar_count=%s send_count=%s', len(toolbar), len(sends))
    if len(toolbar) != 1 or len(sends) != 1:
        raise ValueError(f'无法定位目标聊天编辑区上下边界：工具栏={len(toolbar)}，发送按钮={len(sends)}')
    bounds, top, bottom = window.rectangle(), toolbar[0].rectangle(), sends[0].rectangle()
    y = (top.bottom + bottom.top) // 2
    x = top.left + 30
    if not (bounds.left < x < bounds.right and bounds.top < top.bottom < y < bottom.top < bounds.bottom):
        raise ValueError('目标聊天编辑区边界异常，未输入留言')
    return x - bounds.left, y - bounds.top


def wait_for(operation, message, timeout=15):
    logger.debug('WAIT start: %s (timeout=%ss)', message, timeout)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = operation()
        if value:
            logger.debug('WAIT success: %s', message)
            return value
        time.sleep(.3)
    logger.error('WAIT timeout: %s', message)
    raise RuntimeError(message)


def friend_name(text, account, contact_scope=False):
    suffix = r'(?:来自[:：]\s*我的好友|联系人)'
    if contact_scope:
        # QQ displays the user's custom friend-group name after 来源/来自.
        suffix = r'(?:(?:来自|来源)[:：]\s*[^\r\n]+|联系人)?'
    match = re.fullmatch(r'(.+?)\s*\(' + re.escape(account) + r'\)\s*' + suffix, text.strip())
    return match.group(1).strip() if match else None


def flash_count_matches(texts, count):
    return bool(re.search(r'(?<!\d)' + str(count) + r'\s*项(?!\d)', '\n'.join(texts)))


def collapsed_flash_title(texts, paths):
    compact = lambda value: re.sub(r'\s+', '', value)
    for text in texts:
        value = compact(text)
        if len(paths) == 1 and value == compact(paths[0].name):
            return text
        if len(paths) > 1 and value.startswith(compact(paths[0].stem)) and value.endswith(f'等{len(paths)}项文件'):
            return text
    return None


def file_record_count(messages, filename):
    count = 0
    for group in messages.descendants(control_type='Group'):
        # QQ splits long names across adjacent Text nodes, e.g. qq-send-te + st.jpg.
        pieces = [e.window_text() for e in group.children(control_type='Text')]
        if ''.join(pieces) == filename:
            count += 1
    return count


def flash_record_state(messages, title):
    """Match this batch's unique title inside a flash card, never old filenames."""
    states = []
    for group in messages.descendants(control_type='Group'):
        pieces = [e.window_text() for e in group.descendants(control_type='Text')]
        text = ''.join(pieces)
        if title not in text or 'QQ闪传' not in text:
            continue
        if any(word in text for word in ('发送失败', '上传失败', '传输失败')):
            states.append('failed')
        elif any(word in text for word in ('上传中', '正在上传', '发送中', '正在发送', '等待上传')):
            states.append('pending')
        else:
            states.append('submitted')
    if 'failed' in states:
        return 'failed'
    if 'pending' in states:
        return 'pending'
    return 'submitted' if states else None


def flash_record_count(messages, title):
    matches = []
    for group in messages.descendants(control_type='Group'):
        text = re.sub(r'\s+', '', ''.join(e.window_text() for e in group.descendants(control_type='Text')))
        if re.sub(r'\s+', '', title) in text and 'QQ闪传' in text:
            matches.append(group)
    # Ignore container groups that include multiple message cards.
    return sum(not any(child in matches for child in group.descendants(control_type='Group'))
               for group in matches)


def flash_card_appeared(messages, title, before):
    # A newly posted card means submitted to QQ, not downloaded by the friend.
    return flash_record_count(messages, title) > before and flash_record_state(messages, title) != 'failed'


def flash_batch_cards(messages, paths, title):
    """QQ renders flash attachments as rich-el scene nodes with stable IDs."""
    cards = {}
    normalize = lambda value: re.sub(r'\s+', '', value)
    for group in messages.descendants(control_type='Group'):
        ident = group.element_info.automation_id
        if not ident or not ident.startswith('rich-el__scene_0__'):
            continue
        texts = [e.window_text() for e in group.descendants(control_type='Text')]
        text = ''.join(texts)
        compact = normalize(text)
        if 'QQ闪传' not in compact or not flash_count_matches(texts, len(paths)):
            continue
        # QQ may retain its default title despite an accepted UIA input value.
        default_names = [normalize(p.name if len(paths) == 1 else p.stem + f'等{len(paths)}项文件') for p in paths]
        if not compact.startswith(normalize(title)) and not any(compact.startswith(name) for name in default_names):
            continue
        cards[ident] = text
    return cards


def process_executable(pid):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        value = ctypes.create_unicode_buffer(size.value)
        if kernel.QueryFullProcessImageNameW(handle, 0, value, ctypes.byref(size)):
            return os.path.normcase(value.value)
        return None
    finally:
        kernel.CloseHandle(handle)


def chat_matches(window, nickname):
    # QQ can embed a chat in its main window or retain a merged-window title
    # naming another conversation. Match the active header, not the OS title.
    # The active chat header is a Button; sidebar names are Text nodes.
    visible = lambda elements: [element for element in elements if element.is_visible()]
    files = visible(window.descendants(title='文件弹出菜单', control_type='Button'))
    messages = visible([e for e in window.descendants() if e.window_text() == '消息列表'])
    if len(files) != 1 or len(messages) != 1:
        return False
    headers = visible(window.descendants(title=nickname, control_type='Button'))
    if len(headers) == 1:
        return True
    normalize = lambda text: re.sub(r'\s+', '', text)
    try:
        body = messages[0].rectangle()
        bounds = window.rectangle()
        headers = [element for element in window.descendants()
                   if element.is_visible() and element.element_info.control_type in ('Text','Button')
                   and normalize(element.window_text()) == normalize(nickname)
                   and body.left <= element.rectangle().left < body.right
                   and bounds.top <= element.rectangle().top < body.top]
        return len(headers) == 1
    except (AttributeError, TypeError, RuntimeError):
        return False


def controls_by_id(window, automation_id, control_type=None):
    # auto_id belongs to WindowSpecification; UIAWrapper.descendants in 0.6.9
    # only accepts build_condition filters. Filter the returned IDs explicitly.
    elements = window.descendants(control_type=control_type) if control_type else window.descendants()
    return [e for e in elements
            if e.element_info.automation_id == automation_id]


def owned_flash_confirmation(window):
    """An owned modal can hide the already verified chat header from UIA."""
    matches = []
    for dialog in window.descendants(control_type='Window'):
        sends = [e for e in dialog.descendants(title='闪传发送', control_type='Button') if e.is_visible()]
        recipients = {e.window_text().strip()[3:].strip() for e in dialog.descendants(control_type='Text')
                      if e.window_text().strip().startswith('发送给')}
        if len(sends) == 1 and len(recipients) == 1 and next(iter(recipients)):
            matches.append(dialog)
    return len(matches) == 1


def keyboard_activate(control):
    """Press Enter only after confirming focus is on the intended control."""
    control.set_focus()
    wait_for(control.has_keyboard_focus, '目标控件未取得键盘焦点，已暂停，未按回车', timeout=3)
    control.type_keys('{ENTER}', set_foreground=False)


def qq_process_running():
    import ctypes
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [('size',wintypes.DWORD),('usage',wintypes.DWORD),('pid',wintypes.DWORD),
                    ('heap',ctypes.c_size_t),('module',wintypes.DWORD),('threads',wintypes.DWORD),
                    ('parent',wintypes.DWORD),('priority',wintypes.LONG),('flags',wintypes.DWORD),
                    ('exe',wintypes.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    kernel.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle == ctypes.c_void_p(-1).value:
        raise ValueError('无法检查 QQ 运行状态，请手动唤回 QQ；不会启动新客户端')
    try:
        entry=Entry();entry.size=ctypes.sizeof(entry)
        found=kernel.Process32FirstW(handle,ctypes.byref(entry))
        while found:
            if entry.exe.casefold()=='qq.exe':return True
            found=kernel.Process32NextW(handle,ctypes.byref(entry))
        return False
    finally:
        kernel.CloseHandle(handle)


def restore_hidden_qq():
    """Reveal an existing window without forcing QQ's renderer geometry."""
    import win32gui, win32process, win32con
    handles=[]
    def collect(hwnd,_):
        if win32gui.GetWindowText(hwnd) != 'QQ':return
        pid=win32process.GetWindowThreadProcessId(hwnd)[1]
        if Path(process_executable(pid) or '').name.casefold()=='qq.exe':handles.append(hwnd)
    win32gui.EnumWindows(collect,None)
    for hwnd in handles:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd,win32con.SW_RESTORE)
        elif not win32gui.IsWindowVisible(hwnd):
            win32gui.ShowWindow(hwnd,win32con.SW_SHOW)
    return bool(handles)


class QQSender:
    def __init__(self):
        logger.info('SENDER version=%s flow=%s source=%s', APP_VERSION, QQ_FLOW_VERSION, __file__)
        import ctypes
        deps = ROOT / '.deps'
        for relative in ('', 'win32', 'win32/lib', 'pythonwin', 'pywin32_system32'):
            directory = deps / relative
            sys.path.insert(0, str(directory))
            if directory.is_dir() and hasattr(os, 'add_dll_directory'):
                _dll_dirs.append(os.add_dll_directory(str(directory)))
        # COM must initialize in the worker thread before importing UI Automation.
        sys.coinit_flags = 0
        ctypes.windll.ole32.CoInitializeEx(None, 0)
        from pywinauto import Desktop, Application
        from pywinauto.keyboard import send_keys
        self.hotkey = send_keys
        self.desktop = Desktop(backend='uia')
        self.Application = Application
        self.chat = None

    def connect(self, account):
        logger.info('CONNECT start qq=%s', account)
        if not account.isdigit():
            raise ValueError('无效的 QQ 号')
        def search_field(window):
            # QQ removes the search placeholder/name while the field is active.
            edits = window.descendants(control_type='Edit')
            named = [e for e in edits if e.window_text() == '搜索']
            if len(named) == 1:
                return named[0]
            if len(edits) == 1 and any(e.window_text() == '会话列表' for e in window.descendants()):
                return edits[0]
            return None

        def find_main():
            global _main_identity
            matches = []
            # QQ can change its top-level title after opening a conversation.
            # Identify the logged-in main window by process and search controls.
            for w in self.desktop.windows(visible_only=False):
                if Path(process_executable(w.process_id()) or '').name.casefold() != 'qq.exe':
                    continue
                field = search_field(w)
                if logger.isEnabledFor(10):
                    edits = w.descendants(control_type='Edit')
                    logger.debug('QQ candidate pid=%s handle=%s visible=%s minimized=%s title_is_QQ=%s edits=%s search_found=%s search_visible=%s search_enabled=%s',
                                 w.process_id(), getattr(w, 'handle', None), w.is_visible(), window_minimized(w),
                                 w.window_text() == 'QQ', len(edits), field is not None,
                                 field.is_visible() if field is not None else None,
                                 field.is_enabled() if field is not None else None)
                # After sending, QQ may temporarily remove the search Edit.
                # Its session list still identifies the logged-in main window.
                if field is not None or any(e.window_text() == '会话列表' for e in w.descendants()):
                    matches.append(w)
            logger.debug('MAIN search matches=%s', len(matches))
            selected = select_main_window(matches, _main_identity)
            if selected is not None:
                handle = getattr(selected, 'handle', None)
                if isinstance(handle, int) and handle:
                    _main_identity = (selected.process_id(), handle)
            return selected
        main = find_main()
        if main is not None and window_minimized(main):
            main.restore()
        expanded = main is not None and main.is_visible() and not window_minimized(main)
        logger.debug('MAIN expanded=%s main_found=%s', expanded, main is not None)
        if not expanded and _main_identity is not None:
            recovered = recover_main_window(self.desktop, _main_identity)
            if recovered is not None:
                main = wait_for(find_main, '已按句柄恢复 QQ 主面板，但搜索控件未就绪；未重复切换快捷键', timeout=15)
                expanded = True
        # A detached chat can remain visible while QQ's search main window
        # auto-hides at the screen edge. Only the main window controls wake-up.
        # This shortcut toggles visibility. Repeating it between tasks would
        # hide the already expanded main window and interrupt the next search.
        if not expanded and (main is not None or qq_process_running()):
            logger.info('HOTKEY Ctrl+Alt+X: main not expanded')
            self.hotkey('^%x')
            main = wait_for(find_main, 'Ctrl+Alt+X 未唤起已登录 QQ，请检查 QQ 快捷键设置', timeout=15)
        if main is None:
            if qq_process_running():
                raise ValueError('QQ 已运行但聊天界面不可用；请手动展开现有 QQ 主窗口，空白窗口请关闭后重新打开 QQ。不会启动新登录面板')
            elif self.desktop.windows(title='QQ', visible_only=False):
                raise ValueError('QQ 已运行，但没有找到已登录主窗口；请先登录并打开 QQ 主窗口，不会重复启动客户端')
        if main is None:
            from delivery import open_client
            open_client('qq')
            main = wait_for(find_main, 'QQ 未登录或搜索控件不可用，请打开并登录 QQ')
        if window_minimized(main):
            main.restore()
        main.set_focus()
        logger.info('SEARCH Ctrl+F')
        main.type_keys('^f', set_foreground=True)
        # Restoring an edge-hidden window can replace its renderer/UIA elements.
        # Reacquire the main window and search field after it becomes usable.
        def ready_search():
            refreshed = find_main()
            if refreshed is None:
                return None
            field = search_field(refreshed)
            if field is not None and field.is_visible() and field.is_enabled():
                return refreshed, field
            return None
        main, search = wait_for(ready_search, 'QQ 已唤回，但搜索框尚不可操作；请确认 QQ 主窗口保持展开', timeout=15)
        main.set_focus()
        search.set_focus()
        search.type_keys('^a', set_foreground=True)
        search.type_keys(account, set_foreground=True, pause=.08)
        logger.debug('SEARCH QQ digits entered')
        def entered():
            field = search_field(main)
            return field is not None and field.get_value().strip() == account
        wait_for(entered, 'QQ 搜索框没有收到完整 QQ 号，已暂停；请检查窗口是否再次贴边隐藏', timeout=5)
        def find_friend():
            found = []
            in_contacts = False
            for e in main.descendants():
                text = e.window_text().strip()
                if text == '联系人':
                    in_contacts = True
                    continue
                if text in ('群聊', '群组', '聊天记录', '综合搜索', '搜索网络结果', '频道', '更多搜索结果'):
                    in_contacts = False
                    continue
                name = friend_name(text, account, contact_scope=in_contacts)
                if name:
                    found.append((e, name))
            if len(found) > 1:
                raise ValueError('QQ 搜索结果不唯一，已停止发送')
            return found[0] if found else None
        entry, nickname = wait_for(find_friend, '没有找到 QQ 号完全匹配的“我的好友”；不会发送到群聊或综合搜索结果')
        logger.info('SEARCH exact QQ match found nickname=%s; opening conversation', nickname)
        # Text-only result rows may not expose keyboard focus. Keep one targeted
        # click for those rows rather than pressing Enter into an unknown target.
        try:
            entry.set_focus()
        except (NotImplementedError, RuntimeError):
            entry.click_input()
        else:
            if entry.has_keyboard_focus():
                entry.type_keys('{ENTER}',set_foreground=False)
            else:
                entry.click_input()
        self.qq_executable = process_executable(main.process_id())
        self.qq_pid = main.process_id()
        if not self.qq_executable:
            raise ValueError('无法核对 QQ 进程路径，已停止发送')
        def find_chat():
            import ctypes
            matches = [w for w in self.desktop.windows(visible_only=False)
                       if chat_matches(w, nickname)
                       and w.process_id() == self.qq_pid
                       and process_executable(w.process_id()) == self.qq_executable]
            foreground = ctypes.windll.user32.GetForegroundWindow()
            return select_chat_window(matches, foreground)
        try:
            self.chat = wait_for(find_chat, '未找到当前联系人匹配的 QQ 聊天区域；请检查好友聊天是否打开及聊天顶部名称', timeout=25)
        except RuntimeError:
            for window in self.desktop.windows(visible_only=True):
                if window.process_id() != self.qq_pid:continue
                logger.debug('CHAT rejected expected=%s handle=%s title=%s controls=%s',nickname,window.handle,window.window_text(),
                             [(e.element_info.control_type,e.window_text(),str(e.rectangle()))
                              for e in window.descendants() if e.is_visible()
                              and (e.element_info.control_type=='Button' or e.window_text()=='消息列表')])
            raise
        if any(e.is_visible() for e in self.chat.descendants(title='闪传发送', control_type='Button')):
            raise ValueError('本次目标聊天仍有闪传确认窗口，请核对并关闭后继续')
        self.nickname = nickname
        self.account = account
        logger.info('CONNECT complete qq=%s', account)
        logger.debug('TARGET pinned pid=%s handle=%s', self.qq_pid, getattr(self.chat, 'handle', None))

    def send_file(self, path):
        self.send_files([path])

    def send_message(self, text):
        from delivery import paste_message
        if not chat_matches(self.chat, self.nickname) or process_executable(self.chat.process_id()) != self.qq_executable:
            raise ValueError('留言前聊天对象发生变化')
        self.chat.set_focus()
        try:
            editor = find_chat_input(self.chat)
        except ValueError:
            logger.info('MESSAGE editor has no standard UIA pattern; clicking bounded chat input area')
            point = message_input_point(self.chat)
            self.chat.click_input(coords=point)
            # Stay in the pinned chat; coordinates are relative to its current
            # toolbar and send button, including on secondary monitors.
            paste_message(self.chat, text, self.chat.handle)
            def typed():
                bounds = self.chat.rectangle()
                floor = self.chat.descendants(title='文件弹出菜单', control_type='Button')[0].rectangle().bottom
                for e in self.chat.descendants():
                    rect = e.rectangle()
                    if rect.top >= floor and rect.bottom <= bounds.bottom and e.window_text().strip() == text:
                        return True
                return False
            wait_for(typed, '未确认留言文字已进入编辑区，未点击发送，请核对草稿', timeout=5)
        else:
            if editor.window_text().strip() not in ('', '输入', '输入消息', '消息输入框', '消息输入'):
                raise ValueError('聊天输入框已有草稿，未追加返图留言')
            editor.set_focus()
            wait_for(editor.has_keyboard_focus, '聊天输入框未获得键盘焦点，未输入留言', timeout=3)
            paste_message(editor, text, self.chat.handle)
        def ready_send():
            buttons = [e for e in self.chat.descendants(title='发送')
                       if e.element_info.control_type in ('Button', 'SplitButton')
                       and e.is_visible() and e.is_enabled()]
            return buttons[0] if len(buttons) == 1 else None
        wait_for(ready_send, '无法唯一定位可用的聊天发送按钮', timeout=5).click_input()
        def message_present():
            lists = [e for e in self.chat.descendants() if e.window_text() == '消息列表']
            return len(lists) == 1 and any(e.window_text() == text for e in lists[0].descendants())
        wait_for(message_present, '聊天中未确认本次返图留言', timeout=15)
        logger.info('Completion message confirmed qq=%s', self.account)

    def send_files(self, paths):
        logger.info('FLASH start file_count=%s', len(paths))
        paths = [Path(p).resolve(strict=True) for p in paths]
        if not paths:
            raise ValueError('没有待闪传的文件')
        if (not self.chat or not chat_matches(self.chat, self.nickname)
                or process_executable(self.chat.process_id()) != self.qq_executable):
            raise ValueError('聊天窗口发生变化，已停止发送')
        title = paths[0].name if len(paths) == 1 else paths[0].stem + f'等{len(paths)}项文件'
        initial_lists = [e for e in self.chat.descendants() if e.window_text() == '消息列表']
        if len(initial_lists) != 1:
            raise ValueError('打开闪传前无法定位目标聊天消息列表')
        before = flash_record_count(initial_lists[0], title)
        before_cards = set(flash_batch_cards(initial_lists[0], paths, title))
        self.chat.set_focus()
        menus = self.chat.descendants(title='文件弹出菜单', control_type='Button')
        if len(menus) != 1:
            raise ValueError('无法唯一定位当前聊天的文件菜单，发送队列已暂停')
        menus[0].click_input()
        logger.debug('FLASH file menu clicked')
        def flash_menu():
            items = [e for e in self.chat.descendants(title='闪传文件', control_type='MenuItem')
                     if e.is_visible() and e.is_enabled()]
            return items[0] if len(items) == 1 else None
        wait_for(flash_menu, '未找到当前聊天文件菜单中的“闪传文件”').click_input()
        logger.debug('FLASH transfer menu item clicked')
        def find_dialog():
            dialogs = self.chat.descendants(title='请选择', control_type='Window')
            if not dialogs:
                dialogs = self.desktop.windows(title='请选择', process=self.chat.process_id())
            return dialogs[0] if len(dialogs) == 1 else None
        dialog = wait_for(find_dialog, '点击闪传文件后未出现文件选择窗口；队列已暂停')
        edits = controls_by_id(dialog, '1148', 'Edit')
        confirm = [e for e in controls_by_id(dialog, '1')
                   if e.element_info.control_type in ('Button', 'SplitButton')]
        if len(edits) != 1 or len(confirm) != 1:
            raise ValueError('未找到闪传文件名输入框或确认按钮')
        value = ' '.join('"' + str(p) + '"' for p in paths)
        if len(value) > 30000:
            raise ValueError('文件路径列表过长，请减少单次选片数量')
        edits[0].set_edit_text(value)
        keyboard_activate(confirm[0])
        logger.debug('FLASH file selection confirmed')
        def confirmation():
            if process_executable(self.chat.process_id()) != self.qq_executable:
                raise ValueError('QQ 窗口所属进程变化，已停止发送')
            buttons = [e for e in self.chat.descendants(title='闪传发送', control_type='Button')
                       if e.is_visible() and e.is_enabled()]
            if len(buttons) > 1:
                raise ValueError('当前聊天出现多个闪传发送按钮，已暂停')
            return buttons[0] if buttons else None
        send = wait_for(confirmation, '未找到当前聊天中的闪传发送按钮', timeout=25)
        if window_minimized(self.chat):
            self.chat.restore()
        self.chat.set_focus()
        send = confirmation()
        if send is None:
            raise RuntimeError('QQ 界面尚未就绪，请手动展开 QQ 后继续')
        # QQ's flash confirmation accepts Enter as its default action. Keep
        # focus on the chat owning the visible modal; button focus is unreliable.
        self.chat.type_keys('{ENTER}', set_foreground=False)
        logger.info('FLASH confirmation Enter sent once')
        def confirmation_closed():
            return not any(e.is_visible() for e in self.chat.descendants(title='闪传发送', control_type='Button'))
        wait_for(confirmation_closed, 'QQ 未响应闪传确认窗口的 Enter 发送；队列已暂停，请检查 QQ，未重复发送', timeout=15)
        def submitted():
            if (not (chat_matches(self.chat, self.nickname) or owned_flash_confirmation(self.chat))
                    or process_executable(self.chat.process_id()) != self.qq_executable):
                raise ValueError('核对期间聊天对象发生变化，请核对发送结果')
            lists = [e for e in self.chat.descendants() if e.window_text() == '消息列表']
            state = flash_record_state(lists[0], title) if len(lists) == 1 else None
            if state == 'failed':
                raise ValueError('QQ 本批闪传卡片显示发送或上传失败，请核对发送结果')
            if len(lists) != 1:
                return False
            # A lingering confirmation dialog must never carry into the next chat.
            if any(e.is_visible() for e in self.chat.descendants(title='闪传发送', control_type='Button')):
                return False
            cards = flash_batch_cards(lists[0], paths, title)
            added = set(cards) - before_cards
            if added:
                if any(word in cards[ident] for ident in added for word in ('发送失败','上传失败','传输失败')):
                    raise ValueError('本批新增闪传卡片显示失败，请核对发送结果')
                return True
            return flash_card_appeared(lists[0], title, before)
        wait_for(submitted, '未能确认本批闪传卡片状态；请核对后再重试，避免重复发送', timeout=120)
        logger.info('FLASH current batch card confirmed')
