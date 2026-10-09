"""Targeted WeChat UIA file sending; stop when the contact cannot be verified."""
import ctypes
import re
import time
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
            self.visual.defer_confirmation=getattr(self,"defer_confirmation",False)
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
        time.sleep(1)
        search.type_keys('{ENTER}', set_foreground=False)
        time.sleep(.5)
        self.nickname = None
        def activated_chat():
            handle = ctypes.windll.user32.GetForegroundWindow()
            window = self.desktop.window(handle=handle).wrapper_object()
            if window.process_id() != self.pid:
                return None
            if self.nickname is None:
                bounds = window.rectangle()
                titles = [e.window_text().strip() for e in window.descendants(control_type='Text')
                          if e.is_visible() and e.window_text().strip()
                          and e.rectangle().left > bounds.left + bounds.width()/3
                          and bounds.top <= e.rectangle().top < bounds.top + min(120, bounds.height()/4)]
                if not titles:
                    return None
                self.nickname = titles[0]
            if not self.matches_chat_header(window):
                return None
            return window
        self.chat = wait_for(activated_chat, '微信尚未进入聊天窗口，请确认微信号能搜索到联系人')
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

    def attachment_evidence(self,paths):
        editor=self.editor();bounds=self.chat.rectangle();top=editor.rectangle().top
        messages=[];images=0
        for element in self.chat.descendants():
            if not element.is_visible():continue
            rect=element.rectangle()
            if not (bounds.top+100<rect.top<top and rect.left>bounds.left+bounds.width()*.45):continue
            if element.element_info.control_type=='Text':messages.append(element.window_text())
            if element.element_info.control_type=='Image' and rect.width()>=80 and rect.height()>=60:
                images+=1
        from wechat_visual import archive_label_matches
        return {'files':{Path(path).name:sum(archive_label_matches(text,Path(path).name) for text in messages) for path in paths},
                'images':images,'draft':bool(editor.get_value().strip()),
                'failed':sum(any(word in text for word in ('发送失败','重新发送','网络异常')) for text in messages)}

    def send_files(self, paths):
        if getattr(self, 'visual', None) is not None:
            result=self.visual.send_files(paths)
            self.pending_confirmation=getattr(self.visual,'pending_confirmation',None)
            return result
        from delivery import copy_files, wechat_attachment_paths
        paths=wechat_attachment_paths(paths)
        before=self.attachment_evidence(paths)
        editor = self.editor()
        if editor.get_value().strip():
            raise ValueError('微信输入区有草稿，未粘贴压缩包')
        self.chat.set_focus()
        editor.set_focus()
        wait_for(editor.has_keyboard_focus, '微信编辑区未获得焦点', timeout=3)
        copy_files(paths, self.chat.handle)
        editor.type_keys('^v', set_foreground=False)
        def send_button():
            buttons = [e for e in self.chat.descendants(title='发送', control_type='Button')
                       if e.is_visible() and e.is_enabled()]
            return buttons[0] if len(buttons) == 1 else None
        wait_for(send_button, '微信文件确认发送按钮未就绪').click_input()
        if getattr(self,'defer_confirmation',False):
            self.pending_confirmation={'paths':[str(p) for p in paths],'before':before}
            return
        self.verify_attachment({'paths':[str(p) for p in paths],'before':before})

    def verify_attachment(self,receipt):
        if getattr(self,'visual',None) is not None:
            return self.visual.verify_attachment(receipt)
        paths,before=receipt['paths'],receipt['before']
        from wechat_confirmation import attachment_confirmed
        wait_for(lambda:attachment_confirmed(paths,before,self.attachment_evidence(paths)),
                 '无法确认本次附件已进入聊天记录，请核对后继续；不会自动标记已返图',timeout=20)
        logger.info('WECHAT attachment messages verified count=%s',len(paths))

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
        if getattr(self,'defer_confirmation',False):return
        wait_for(lambda: any(e.window_text() == text for e in self.chat.descendants()) and not editor.get_value().strip(),
                 '微信留言未确认', timeout=15)
