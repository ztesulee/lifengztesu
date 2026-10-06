"""Targeted WeChat UIA file sending; stop when the contact cannot be verified."""
import ctypes
import re
from pathlib import Path
from qq_sender import QQSender, process_executable, window_minimized, wait_for
from debug_log import logger


def exact_wechat_result(text, account):
    return bool(re.search(r'微信号\s*[:：]\s*' + re.escape(account) + r'(?=$|\s)', text))


def wake_wechat(find_main, hotkey):
    main = find_main()
    if main is not None and window_minimized(main):
        main.restore()
    if main is not None and main.is_visible():
        main.set_focus()
        logger.debug('WECHAT reused visible main window handle=%s', main.handle)
        return main
    logger.info('WECHAT waking hidden main window with Ctrl+Alt+W')
    hotkey('^%w')
    def visible_main():
        candidate = find_main()
        return candidate if candidate is not None and candidate.is_visible() and not window_minimized(candidate) else None
    main = wait_for(visible_main, 'Ctrl+Alt+W 未呼出微信，请确认微信已登录及快捷键设置；未启动新的微信进程', timeout=15)
    main.set_focus()
    return main


class WeChatSender(QQSender):
    def connect(self, account):
        self.account = account.strip()
        if not self.account:
            raise ValueError('请先填写微信接收账号')
        logger.info('WECHAT connect account=%s', self.account)
        def main_window():
            matches = []
            for window in self.desktop.windows(visible_only=False):
                exe = Path(process_executable(window.process_id()) or '').name.casefold()
                if exe not in ('weixin.exe', 'wechat.exe'):
                    continue
                if window.window_text() in ('微信', 'Weixin', 'WeChat'):
                    matches.append(window)
            if len(matches) > 1:
                raise ValueError('发现多个微信主窗口，无法确定发送账号')
            return matches[0] if matches else None
        main = wake_wechat(main_window, self.hotkey)
        if not main.descendants(control_type='Edit'):
            from wechat_visual import WeChatVisualSender
            self.visual = WeChatVisualSender(main)
            self.visual.connect(self.account)
            return
        self.pid = main.process_id()
        main.set_focus()
        main.type_keys('^f', set_foreground=False)
        def search_field():
            fields = [e for e in main.descendants(control_type='Edit')
                      if e.is_visible() and e.is_enabled() and e.window_text() in ('搜索', '搜索联系人')]
            return fields[0] if len(fields) == 1 else None
        search = wait_for(search_field, '微信搜索框无法定位，请展开微信主窗口')
        search.set_focus()
        search.type_keys('^a', set_foreground=False)
        from delivery import paste_message
        paste_message(search, self.account, main.handle)
        wait_for(lambda: search.get_value().strip() == self.account, '微信搜索框未收到完整账号', timeout=5)
        def contact():
            rows = []
            for kind in ('ListItem', 'Group'):
                for row in main.descendants(control_type=kind):
                    texts = [row.window_text()] + [e.window_text() for e in row.descendants(control_type='Text')]
                    if exact_wechat_result('\n'.join(texts), self.account) and row.is_visible():
                        rows.append((row, texts))
                if rows:
                    break
            # Nested groups repeat the same contact; the smallest verified row
            # excludes ancestor panels containing several unrelated results.
            if not rows:
                return None
            areas = [(r.rectangle().width()*r.rectangle().height(), r, texts) for r, texts in rows]
            if not any(a > 0 for a, _, _ in areas):
                return None
            smallest = min(a for a, _, _ in areas if a > 0)
            chosen = [(r, t) for a, r, t in areas if a == smallest]
            if len(chosen) != 1:
                raise ValueError('微信号精确匹配结果不唯一，未发送')
            return chosen[0]
        row, texts = wait_for(contact, '微信结果未显示完全匹配的微信号；未按昵称或相似联系人发送')
        labels = [t.strip() for t in texts if t.strip() and not exact_wechat_result(t, self.account)
                  and t.strip() not in ('联系人', self.account)]
        if not labels:
            raise ValueError('无法核对微信联系人名称，未发送')
        self.nickname = labels[0]
        row.click_input()
        def activated_chat():
            handle = ctypes.windll.user32.GetForegroundWindow()
            window = self.desktop.window(handle=handle).wrapper_object()
            if window.process_id() != self.pid:
                return None
            if not self.matches_chat_header(window):
                return None
            return window
        self.chat = wait_for(activated_chat, '微信未打开已核对联系人聊天')
        logger.info('WECHAT target pinned pid=%s handle=%s', self.pid, self.chat.handle)

    def matches_chat_header(self, window):
        bounds = window.rectangle()
        return any(e.window_text() == self.nickname
                   and e.rectangle().left > bounds.left + bounds.width()/3
                   and bounds.top <= e.rectangle().top < bounds.top + min(120, bounds.height()/4)
                   for e in window.descendants())

    def editor(self):
        if self.chat.process_id() != self.pid or not self.matches_chat_header(self.chat):
            raise ValueError('微信目标聊天发生变化，已停止发送')
        editors = [e for e in self.chat.descendants(control_type='Edit')
                   if e.is_visible() and e.is_enabled() and e.window_text() not in ('搜索', '搜索联系人')]
        if len(editors) != 1:
            raise ValueError('无法唯一定位微信聊天编辑区')
        return editors[0]

    def send_files(self, paths):
        if getattr(self, 'visual', None) is not None:
            return self.visual.send_files(paths)
        from delivery import copy_files
        if len(paths) != 1 or Path(paths[0]).suffix.lower() != '.zip':
            raise ValueError('微信仅发送本任务生成的一个 ZIP')
        path = Path(paths[0]).resolve(strict=True)
        editor = self.editor()
        if editor.get_value().strip():
            raise ValueError('微信输入区有草稿，未粘贴压缩包')
        before = sum(e.window_text() == path.name for e in self.chat.descendants())
        self.chat.set_focus()
        editor.set_focus()
        wait_for(editor.has_keyboard_focus, '微信编辑区未获得焦点', timeout=3)
        copy_files([path], self.chat.handle)
        editor.type_keys('^v', set_foreground=False)
        def send_button():
            buttons = [e for e in self.chat.descendants(title='发送', control_type='Button')
                       if e.is_visible() and e.is_enabled()]
            return buttons[0] if len(buttons) == 1 else None
        wait_for(send_button, '微信文件确认发送按钮未就绪').click_input()
        logger.info('WECHAT ZIP submitted; upload continues in client; advancing queue')

    def send_message(self, text):
        if getattr(self, 'visual', None) is not None:
            return self.visual.send_message(text)
        editor = self.editor()
        if editor.get_value().strip():
            raise ValueError('微信输入区有草稿，未追加留言')
        self.chat.set_focus()
        editor.set_focus()
        wait_for(editor.has_keyboard_focus, '微信留言编辑区未获得焦点', timeout=3)
        from delivery import paste_message
        paste_message(editor, text, self.chat.handle)
        buttons = [e for e in self.chat.descendants(title='发送', control_type='Button') if e.is_visible() and e.is_enabled()]
        if len(buttons) != 1:
            raise ValueError('微信留言发送按钮不唯一')
        buttons[0].click_input()
        wait_for(lambda: any(e.window_text() == text for e in self.chat.descendants()) and not editor.get_value().strip(),
                 '微信留言未确认', timeout=15)
