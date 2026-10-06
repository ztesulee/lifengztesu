"""摄影返图工作台 — local-first project, review, selection and packaging."""
import copy
import hashlib
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

ROOT = Path(__file__).resolve().parent
SESSION_FILE = ROOT / '.last-project.json'
sys.path.insert(0, str(ROOT / '.deps'))
from media import collect, fingerprint, transcode, prepare_web_previews, import_media, PHOTOS, VIDEOS, RAW
from project import Project, person_name_error, person_recipient, task_recipient
from server import AlbumServer
from media_grid import MediaGrid
from app_version import APP_VERSION, QQ_FLOW_VERSION
from debug_log import logger
from recent_projects import read_recent, remember

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'摄影返图工作台 · {APP_VERSION} 原片返图')
        logger.info('APP version=%s QQ_flow=%s', APP_VERSION, QQ_FLOW_VERSION)
        self.geometry('1250x820')
        self.minsize(1000, 670)
        self.configure(bg='#f3f4ef')
        self.project = None
        self.server = None
        self.busy = False
        self.events = queue.Queue()
        self.images = {}
        self.group_ids = []
        self.current_group = '*'
        self.page = 0
        self.auto_qq = tk.BooleanVar(value=False)
        self.return_mode = tk.StringVar(value='web')
        self.delivery_active = False
        self.delivery_channel = tk.StringVar(value='人物默认')
        self.drag_start = None
        self.drag_ids = set()
        self.drag_hover = None
        self.drag_active = False
        self.status = tk.StringVar(value='新建项目 → 建立人物文件夹 → 添加素材 → 发布选片相册。')
        self.project_title = tk.StringVar(value='摄影返图工作台')
        self.public_url = tk.StringVar()
        from openfrp_client import OpenFrpClient
        self.openfrp = OpenFrpClient()
        self.openfrp_status = tk.StringVar(value='已配置，点击一键启动' if self.openfrp.settings.get('config') and self.openfrp.settings.get('url') else
                                         '客户端已安装，待配置隧道' if self.openfrp.settings.get('executable') else 'OpenFrp 未配置')
        self.after(500, self.poll_openfrp)
        self.link = tk.StringVar()
        self.detail = tk.StringVar(value='选择素材查看原文件路径。')
        self.summary = tk.StringVar(value='尚未打开项目')
        self.page_info = tk.StringVar()
        self.task_progress_text = tk.StringVar(value='就绪')
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#f3f4ef')
        style.configure('TButton', padding=(10, 7))
        style.configure('Treeview', rowheight=76, background='white', fieldbackground='white')
        style.configure('Treeview.Heading', padding=7, font=('Microsoft YaHei UI', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#cce5dc')], foreground=[('selected', '#12382d')])
        style.configure('Small.Treeview', rowheight=32)
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 23, 'bold'), foreground='#183e35')
        self.build()
        self.history_scan_active = False
        self.history_report = None
        self.after(1500, self.start_history_scan)
        self.after(300000, self.periodic_history_scan)
        self.after(100, self.pump)
        self.after(2000, self.poll_results)
        self.protocol('WM_DELETE_WINDOW', self.close)

    def build(self):
        header = ttk.Frame(self, padding=(22, 18))
        header.pack(fill='x')
        ttk.Label(header, textvariable=self.project_title, style='Title.TLabel').pack(side='left')
        for text, command in [('打开项目', self.open_project), ('新建拍摄项目', self.new_project)]:
            ttk.Button(header, text=text, command=command).pack(side='right', padx=4)
        self.recent_button = ttk.Menubutton(header, text='最近项目 ▾')
        self.recent_button.pack(side='right', padx=4)
        self.recent_menu = tk.Menu(self.recent_button, tearoff=False)
        self.recent_button.configure(menu=self.recent_menu)
        self.refresh_recent_projects()
        ttk.Button(header, text='历史人物清单', command=self.open_history_report).pack(side='right', padx=4)
        ttk.Label(self, text='  01 建立人物文件夹    →    02 添加照片 / 视频    →    03 模特选片    →    04 QQ 自动返图', padding=(20, 0)).pack(anchor='w')
        ttk.Label(self, textvariable=self.status, padding=(20, 12), wraplength=1150).pack(side='bottom', fill='x')
        progress_area = ttk.Frame(self, padding=(20, 4))
        progress_area.pack(side='bottom', fill='x')
        ttk.Label(progress_area, textvariable=self.task_progress_text, width=38).pack(side='left')
        self.task_progress_bar = ttk.Progressbar(progress_area, maximum=100, mode='determinate')
        self.task_progress_bar.pack(side='left', fill='x', expand=True, padx=(8, 0))
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill='both', expand=True, padx=20, pady=14)
        review = ttk.Frame(self.tabs, padding=12)
        review.columnconfigure(0, weight=1)
        review.rowconfigure(2, weight=1)
        share = ttk.Frame(self.tabs, padding=22)
        results = ttk.Frame(self.tabs, padding=16)
        self.tabs.add(review, text='  人物与素材  ')
        self.tabs.add(results, text='  选片与发送任务  ')
        self.tabs.add(share, text='  网络设置  ')
        toolbar = ttk.Frame(review)
        toolbar.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        modes = ttk.Frame(toolbar)
        modes.pack(fill='x', pady=(0,8))
        ttk.Label(modes,text='返图模式：').pack(side='left')
        ttk.Radiobutton(modes,text='网页选片',variable=self.return_mode,value='web',command=self.change_return_mode).pack(side='left',padx=8)
        ttk.Radiobutton(modes,text='直接返图（全部素材）',variable=self.return_mode,value='direct',command=self.change_return_mode).pack(side='left',padx=8)
        toolbar_buttons=ttk.Frame(toolbar)
        toolbar_buttons.pack(fill='x')
        toolbar=toolbar_buttons
        for label, command in [('新建人物文件夹', self.new_group), ('重命名人物', self.rename_group), ('添加文件夹', self.import_folder), ('添加文件', self.import_files), ('打开人物文件夹', self.open_group_folder)]:
            ttk.Button(toolbar, text=label, command=command).pack(side='left', padx=(0, 6))
        ttk.Button(toolbar, text='绑定 QQ / 微信', command=self.bind_contact).pack(side='left')
        ttk.Label(review, textvariable=self.summary, wraplength=900).grid(row=1, column=0, sticky='w', pady=(0, 8))
        paned = ttk.Panedwindow(review, orient='horizontal')
        paned.grid(row=2, column=0, sticky='nsew')
        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=3)
        self.after_idle(lambda:paned.sashpos(0, 290))
        ttk.Label(left, text='人物文件夹', font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w', pady=6)
        self.groups = tk.Listbox(left, width=23, selectmode='extended', font=('Microsoft YaHei UI', 11), bg='white', relief='flat', activestyle='none', exportselection=False)
        self.groups.pack(fill='both', expand=True, padx=(0, 10))
        group_scroll = ttk.Scrollbar(left, orient='horizontal', command=self.groups.xview)
        self.groups.configure(xscrollcommand=group_scroll.set)
        group_scroll.pack(fill='x', padx=(0,10))
        ttk.Label(left, text='当前相册链接').pack(anchor='w', pady=(8,2))
        ttk.Entry(left, textvariable=self.link, state='readonly').pack(fill='x', padx=(0,10))
        link_actions=ttk.Frame(left)
        link_actions.pack(fill='x', pady=6)
        for label,cmd in [('复制',self.copy_link),('二维码',self.show_qr),('预览',self.open_local)]:
            ttk.Button(link_actions,text=label,command=cmd).pack(side='left',padx=(0,3))
        self.groups.bind('<<ListboxSelect>>', self.choose_group)
        self.groups.bind('<ButtonRelease-1>', lambda event:self.after_idle(self.choose_group))
        for shortcut in ('<Control-a>', '<Control-A>', '<Alt-a>', '<Alt-A>'):
            self.groups.bind(shortcut, self.select_all_people)
        self.groups.bind('<Button-3>', self.person_menu)
        self.person_context = tk.Menu(self, tearoff=False)
        grid_tools=ttk.Frame(right)
        grid_tools.pack(fill='x', pady=(0,6))
        ttk.Label(grid_tools,text='素材预览 · 双击查看大图').pack(side='left')
        self.thumb_size=tk.StringVar(value='中')
        size_box=ttk.Combobox(grid_tools,textvariable=self.thumb_size,values=('小','中','大'),state='readonly',width=5)
        size_box.pack(side='right')
        ttk.Label(grid_tools,text='缩略图尺寸').pack(side='right',padx=6)
        size_box.bind('<<ComboboxSelected>>',self.resize_thumbnails)
        self.tree = MediaGrid(right)
        scroll = ttk.Scrollbar(right, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select_item)
        for shortcut in ('<Control-a>', '<Control-A>', '<Alt-a>', '<Alt-A>'):
            self.tree.bind(shortcut, self.select_all_media)
        self.tree.bind('<Double-1>', self.preview_item)
        self.tree.bind('<Button-3>', self.media_context_menu)
        ttk.Button(grid_tools, text='重新定位原片', command=self.relocate_sources).pack(side='left', padx=8)
        ttk.Button(grid_tools, text='同步素材文件夹', command=self.sync_source_folder).pack(side='left')
        ttk.Button(grid_tools, text='刷新素材', command=self.refresh_sources).pack(side='left', padx=6)
        ttk.Button(grid_tools,text='检测重复',command=self.check_duplicate_media).pack(side='left')
        self.tree.bind('<ButtonPress-1>', self.begin_drag)
        self.tree.bind('<B1-Motion>', self.drag_motion)
        self.tree.bind('<ButtonRelease-1>', self.end_drag)
        self.bind('<Escape>', lambda event:self.clear_drag())
        nav = ttk.Frame(review)
        nav.grid(row=3, column=0, sticky='ew', pady=5)
        ttk.Label(nav, textvariable=self.page_info).pack(side='left')
        ttk.Label(review, textvariable=self.detail, wraplength=940).grid(row=4, column=0, sticky='w', pady=5)
        actions = ttk.Frame(review)
        actions.grid(row=5, column=0, sticky='ew')
        for label, command in [('修改选中素材归属', self.assign_items), ('移出项目', self.remove_items), ('复制原片到当前人物文件夹', self.copy_originals)]:
            ttk.Button(actions, text=label, command=command).pack(side='left', padx=(0, 6))

        publish_row=ttk.Frame(review)
        self.publish_controls=publish_row
        publish_row.grid(row=6,column=0,sticky='ew',pady=(10,0))
        publish_menu=tk.Menu(self,tearoff=False)
        publish_menu.add_command(label='生成当前人物链接',command=self.publish)
        publish_menu.add_command(label='生成选中人物链接',command=self.publish_batch)
        publish_menu.add_command(label='生成全部人物链接',command=self.publish_all)
        ttk.Menubutton(publish_row,text='生成人物链接 ▾',menu=publish_menu).pack(side='left')
        ttk.Button(publish_row,text='链接清单',command=self.show_album_links).pack(side='left',padx=6)
        ttk.Button(publish_row,text='停止发布当前人物',command=self.unpublish_group).pack(side='left')
        ttk.Label(publish_row,text='先整理素材，再生成链接；修改素材后需重新生成。').pack(side='left',padx=12)
        self.direct_controls=ttk.Frame(review)
        self.direct_controls.grid(row=6,column=0,sticky='ew',pady=(10,0))
        ttk.Button(self.direct_controls,text='发送当前人物全部素材',command=lambda:self.send_direct(False)).pack(side='left')
        ttk.Button(self.direct_controls,text='发送选中人物全部素材',command=lambda:self.send_direct(True)).pack(side='left',padx=8)
        ttk.Button(self.direct_controls,text='发送已选中素材',command=lambda:self.send_direct(selected_only=True)).pack(side='left',padx=(0,8))
        ttk.Label(self.direct_controls,text='无需发布网页或启动 OpenFrp；点击后加入 QQ 发送队列。').pack(side='left')
        self.direct_controls.grid_remove()
        ttk.Label(share,text='网络设置',style='Title.TLabel').pack(anchor='w')
        ttk.Label(share,textvariable=self.openfrp_status,font=('Microsoft YaHei UI',13,'bold'),wraplength=950).pack(anchor='w',pady=16)
        ttk.Label(share, text='公网入口地址（例如 https://你的域名；未配置时仅本机可访问）').pack(anchor='w', pady=(22, 6))
        row = ttk.Frame(share)
        row.pack(fill='x')
        ttk.Entry(row, textvariable=self.public_url).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='保存入口', command=self.save_public_url).pack(side='left', padx=8)
        ttk.Button(row, text='入口配置说明', command=self.network_help).pack(side='left')
        ttk.Label(share, text='入口服务需转发至 http://127.0.0.1:17860。分享期间请保持软件运行、电脑联网且不休眠。', wraplength=1050).pack(anchor='w', pady=8)
        tunnel_row = ttk.Frame(share)
        tunnel_row.pack(fill='x', pady=4)
        for text, command in [('配置 OpenFrp', self.configure_openfrp), ('一键启动 OpenFrp', self.start_openfrp), ('停止 OpenFrp', self.stop_openfrp)]:
            ttk.Button(tunnel_row, text=text, command=command).pack(side='left', padx=(0, 8))
        ttk.Label(tunnel_row, textvariable=self.openfrp_status, wraplength=460).pack(side='left')
        ttk.Label(share,text='首次配置后，日常只需启动 OpenFrp。\n公网入口用于预览和选片；原片打包后通过 QQ / 微信发送。',wraplength=950).pack(anchor='w',pady=22)

        ttk.Label(results, text='原片发送任务', style='Title.TLabel').pack(anchor='w', pady=(0, 12))
        ttk.Label(results, text='QQ：闪传直接发送原片；微信：打包 ZIP 后发送。请保持客户端登录，自动发送时不要操作鼠标键盘。').pack(anchor='w', pady=(0, 12))
        self.results = ttk.Treeview(results, columns=('person', 'created', 'count', 'status', 'recipient'), show='headings', style='Small.Treeview')
        self.results.bind('<Button-3>',self.task_return_menu)
        self.results.bind('<Motion>',self.task_hover)
        self.results.bind('<Leave>',lambda event:self.hide_task_tip())
        self.results.bind('<ButtonPress-1>',lambda event:self.hide_task_tip())
        self.results.bind('<MouseWheel>',lambda event:self.hide_task_tip())
        for col, label, width in [('person', '人物', 110), ('created', '提交时间', 180), ('count', '数量', 65), ('status', '发送状态', 180), ('recipient', '接收账号 / 发送进度', 440)]:
            self.results.heading(col, text=label)
            self.results.column(col, width=width)
        row = ttk.Frame(results)
        row.pack(side='bottom', fill='x', pady=12)
        channel_row = ttk.Frame(results)
        channel_row.pack(side='bottom', fill='x', pady=(6, 0))
        ttk.Checkbutton(channel_row, text='网页提交后自动返图', variable=self.auto_qq, command=self.save_auto_qq).pack(side='left', padx=8)
        self.results.pack(fill='both', expand=True)
        ttk.Label(channel_row, text='发送渠道：').pack(side='left')
        channel_box = ttk.Combobox(channel_row, textvariable=self.delivery_channel, values=('人物默认', 'QQ', '微信'), state='readonly', width=8)
        channel_box.pack(side='left', padx=(0, 6))
        channel_box.bind('<<ComboboxSelected>>', self.apply_delivery_channel)
        ttk.Button(row, text='发送 / 继续选中任务', command=self.queue_delivery).pack(side='left')
        ttk.Button(row, text='核对中断文件', command=self.review_delivery).pack(side='left', padx=8)
        ttk.Button(row, text='删除选中任务', command=self.delete_delivery_tasks).pack(side='left')
        ttk.Button(row, text='打开项目目录', command=self.open_project_folder).pack(side='left')
        ttk.Button(row, text='微信压缩包辅助发送', command=lambda:self.prepare_delivery('wechat')).pack(side='left')

    def change_return_mode(self):
        direct = self.return_mode.get() == 'direct'
        if direct:
            self.publish_controls.grid_remove()
            self.direct_controls.grid()
        else:
            self.direct_controls.grid_remove()
            self.publish_controls.grid()
        if self.project:
            with self.project.lock:
                self.project.data['return_mode'] = self.return_mode.get()
                self.project.save()

    def send_direct(self, multiple=False, selected_only=False):
        if not self.require():
            return
        item_ids=list(self.tree.selection()) if selected_only else None
        if selected_only and not item_ids:
            messagebox.showinfo('选择素材','请先在预览区域选中要发送的素材，可用 Ctrl / Shift 多选。',parent=self)
            return
        if multiple:
            gids=[self.group_ids[i] for i in self.groups.curselection() if self.group_ids[i] in self.project.data['groups']]
        else:
            gid=self.selected_group()
            gids=[gid] if gid else []
        if not gids:
            messagebox.showinfo('选择人物','请先选中要返图的人物文件夹。',parent=self)
            return
        try:
            records=self.project.queue_direct_albums(gids, {'人物默认':'default','QQ':'qq','微信':'wechat'}[self.delivery_channel.get()],item_ids=item_ids)
        except (ValueError,OSError) as exc:
            messagebox.showerror('无法直接返图',str(exc),parent=self)
            return
        self.render_results(self.project.snapshot())
        self.tabs.select(1)
        self.results.selection_set([r['id'] for r in records])
        self.status.set(('选中素材' if selected_only else '全部素材')+'任务已加入队列；相同内容的已有任务会复用，不会重复新建。')
        self.poll_delivery()

    def resize_thumbnails(self,event=None):
        self.tree.card_width={'小':160,'中':210,'大':280}[self.thumb_size.get()]
        selected=self.tree.selection()
        if self.project:
            self.render_items(self.project.snapshot())
            self.tree.selection_set(selected)

    def publish_all(self):
        if not self.require():
            return
        self.groups.selection_clear(0,'end')
        for index,gid in enumerate(self.group_ids):
            if gid in self.project.data['groups']:
                self.groups.selection_set(index)
        self.publish_batch()

    def bind_contact(self):
        gid = self.selected_group()
        if not gid:
            return
        window = self.new_popup()
        window.title('绑定人物 QQ / 微信号')
        box = ttk.Frame(window, padding=20)
        box.pack(fill='both', expand=True)
        contacts = self.project.data['groups'][gid].get('contacts', {})
        qq = tk.StringVar(value=contacts.get('qq', ''))
        wechat = tk.StringVar(value=contacts.get('wechat', ''))
        default = tk.StringVar(value='微信' if self.project.data['groups'][gid].get('default_channel') == 'wechat' or (not contacts.get('qq') and contacts.get('wechat')) else 'QQ')
        ttk.Label(box, text='QQ 和微信可同时保存；每次任务只向所选渠道发送。').pack(anchor='w', pady=(0,12))
        for label, variable in [('QQ 号',qq),('微信号',wechat)]:
            ttk.Label(box,text=label+'（留空解除该账号）').pack(anchor='w',pady=(8,4))
            ttk.Entry(box,textvariable=variable,width=38).pack(fill='x')
        ttk.Label(box,text='人物默认发送渠道').pack(anchor='w',pady=(12,4))
        ttk.Combobox(box,textvariable=default,values=('QQ','微信'),state='readonly').pack(fill='x')
        def save():
            values = {'qq':qq.get().strip(),'wechat':wechat.get().strip()}
            if values['qq'] and not values['qq'].isdigit():
                messagebox.showerror('QQ 号格式','QQ 号只能包含数字。',parent=window)
                return
            chosen = 'qq' if default.get() == 'QQ' else 'wechat'
            available = [key for key, value in values.items() if value]
            if len(available) == 1:
                chosen = available[0]
            with self.project.lock:
                group = self.project.data['groups'][gid]
                group['contacts'] = {k:v for k,v in values.items() if v}
                group['default_channel'] = chosen
                group.pop('legacy_contacts',None)
                self.project.save()
            window.destroy()
            self.refresh(preserve_view=True)
            self.status.set('人物账号和默认发送渠道已保存。')
            self.after(1500, self.start_history_scan)
        ttk.Button(box, text='保存账号', command=save).pack(pady=(16, 0))
        self.place_popup(window)
        window.grab_set()

    def prepare_delivery(self, channel):
        if not self.require() or not self.results.selection():
            return
        from delivery import copy_files, open_client
        record = next(s for s in self.project.snapshot()['submissions'] if s['id'] == self.results.selection()[0])
        account = record.get('recipient', {}).get(channel, '')
        if not account:
            messagebox.showinfo('请先绑定账号', '请在人物与素材页选中此人物，点击“绑定 QQ / 微信”。', parent=self)
            return
        project = self.project
        self.run(lambda progress: project.prepare_archive(record['id'], channel),
                 lambda archive: self.show_archive_handoff(record, channel, account, archive))

    def show_archive_handoff(self, record, channel, account, archive):
        from delivery import copy_files, open_client
        files = [archive]
        window = self.new_popup()
        window.title('QQ 返图压缩包发送' if channel == 'qq' else '微信返图压缩包发送')
        box = ttk.Frame(window, padding=20)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text=f'接收人物：{record["name"]}\n账号：{account}\n压缩包包含 {len(record["files"])} 个原片', wraplength=500).pack(anchor='w')
        listing = tk.Listbox(box, width=65, height=10, exportselection=False)
        listing.pack(fill='both', expand=True, pady=12)
        for path in files:
            listing.insert('end', path.name)
        if files:
            listing.selection_set(0)
        ttk.Label(box, text='辅助发送：打开客户端，复制账号并搜索联系人。\n核对联系人后，复制 ZIP，在聊天框 Ctrl+V 并发送。\n自动批量发送请使用任务列表的“发送 / 继续选中任务”。', wraplength=500).pack(anchor='w')
        feedback = tk.StringVar(value='待发送：请核对接收账号。')
        ttk.Label(box, textvariable=feedback, wraplength=500).pack(anchor='w', pady=8)
        def perform(action):
            try:
                action()
            except Exception as exc:
                messagebox.showerror('操作失败', str(exc), parent=window)
        def copy_account():
            self.clipboard_clear()
            self.clipboard_append(account)
            feedback.set('账号已复制。请在客户端搜索并核对联系人。')
        def copy_originals(all_files=False):
            selected = list(listing.curselection())
            if not all_files and not selected:
                return
            paths = files if all_files else [files[selected[0]]]
            copy_files(paths, window.winfo_id())
            feedback.set('已复制返图 ZIP，尚未发送。请到对应聊天窗口粘贴并发送。')
        buttons = ttk.Frame(box)
        buttons.pack(fill='x')
        for label, action in [('打开客户端', lambda:open_client(channel)), ('复制账号', copy_account),
                              ('复制返图 ZIP', lambda:copy_originals(True))]:
            ttk.Button(buttons, text=label, command=lambda a=action:perform(a)).pack(side='left', padx=3)
        self.place_popup(window)

    def new_popup(self):
        window = tk.Toplevel(self)
        window.withdraw()
        window.transient(self)
        return window

    def place_popup(self, window):
        """Center owned windows on the owner's monitor, including negative coordinates."""
        self.update_idletasks()
        window.update_idletasks()
        width = max(window.winfo_reqwidth(), window.winfo_width())
        height = max(window.winfo_reqheight(), window.winfo_height())
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 2
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                            ('work', wintypes.RECT), ('flags', wintypes.DWORD)]
            user = ctypes.WinDLL('user32', use_last_error=True)
            user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user.MonitorFromWindow.restype = wintypes.HANDLE
            user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            user.GetParent.argtypes = [wintypes.HWND]
            user.GetParent.restype = wintypes.HWND
            user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            monitor = user.MonitorFromWindow(self.winfo_id(), 2)
            if user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                x = max(info.work.left, min(x, info.work.right - width - 16))
                y = max(info.work.top, min(y, info.work.bottom - height - 40))
            window.geometry(f'{width}x{height}')
            window.update_idletasks()
            hwnd = user.GetParent(window.winfo_id()) or window.winfo_id()
            # SetWindowPos uses absolute signed coordinates; Tk's -x geometry uses
            # distance from the screen edge and breaks on monitors left of primary.
            user.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0015)
        else:
            window.geometry(f'{width}x{height}+{max(0,x)}+{max(0,y)}')
        window.deiconify()
        if sys.platform == 'win32':
            user.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0015)
        window.lift()

    def ask_name(self, title, prompt, initialvalue='', validator=None):
        window = self.new_popup()
        window.title(title)
        result = [None]
        box = ttk.Frame(window, padding=20)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text=prompt).pack(anchor='w', pady=(0, 8))
        value = tk.StringVar(value=initialvalue)
        entry = ttk.Entry(box, textvariable=value, width=38)
        entry.pack(fill='x')
        def accept(event=None):
            if validator:
                error = validator(value.get())
                if error:
                    messagebox.showwarning('请修改名称', error, parent=window)
                    entry.focus_set()
                    entry.selection_range(0, 'end')
                    return
            result[0] = value.get()
            window.destroy()
        buttons = ttk.Frame(box)
        buttons.pack(fill='x', pady=(16, 0))
        ttk.Button(buttons, text='确定', command=accept).pack(side='right')
        ttk.Button(buttons, text='取消', command=window.destroy).pack(side='right', padx=8)
        window.bind('<Return>', accept)
        window.bind('<Escape>', lambda event:window.destroy())
        self.place_popup(window)
        window.grab_set()
        entry.focus_set()
        entry.selection_range(0, 'end')
        self.wait_window(window)
        return result[0]

    def require(self):
        if not self.project:
            messagebox.showinfo('先创建项目', '请先新建或打开拍摄项目。', parent=self)
            return False
        if self.busy:
            messagebox.showinfo('正在处理', '请等待当前素材处理完成。', parent=self)
            return False
        return True

    def set_project(self, folder, title=None):
        if self.server:
            self.server.close()
            self.server = None
        if self.project:
            self.project.close()
        self.project = Project(folder, title)
        # Preserve reviewed manual assignments; legacy AI candidates become unassigned.
        if self.project.data.get('workflow') != 'manual':
            backup = self.project.folder / 'backups'
            backup.mkdir(exist_ok=True)
            shutil.copy2(self.project.file, backup / ('before_manual_' + time.strftime('%Y%m%d_%H%M%S') + '.json'))
            for item in self.project.data['items']:
                if not item.get('reviewed'):
                    item['groups'] = []
                item['note'] = '手动人物相册' if item.get('reviewed') else '请选择素材并手动指定人物'
                for key in ('observations', 'clothes', 'appearances', 'frames', 'analysis_model'):
                    item.pop(key, None)
            self.project.data.pop('recognition_option', None)
            self.project.data['workflow'] = 'manual'
            self.project.unpublish()
            self.project.save()
            self.project.manifests()
        for group in self.project.data['groups'].values():
            if not group.get('contacts') and group.get('legacy_contacts'):
                group['contacts'] = group.pop('legacy_contacts')
            if not group.get('default_channel'):
                group['default_channel'] = 'qq' if group.get('contacts', {}).get('qq') else 'wechat'
        self.project.save()
        self.auto_qq.set(bool(self.project.data.get('auto_qq', False)))
        self.return_mode.set(self.project.data.get('return_mode', 'web'))
        self.change_return_mode()
        self.project_title.set(self.project.data['title'])
        self.public_url.set(self.project.data.get('public_url', ''))
        self.current_group = '*'
        self.page = 0
        self.refresh()
        if any(g['published'] for g in self.project.data['groups'].values()):
            self.ensure_server()
        self.status.set('项目已打开。所有更改自动保存。')
        self.remember_project()
        if hasattr(self, 'history_scan_active'):
            self.after(1500, self.start_history_scan)

    def remember_project(self):
        if not self.project:
            return
        try:
            remember(SESSION_FILE, self.project.folder)
            self.refresh_recent_projects()
        except OSError:
            self.status.set('项目已保存，但无法记录下次启动的项目路径。')

    def refresh_recent_projects(self):
        self.recent_menu.delete(0, 'end')
        folders = read_recent(SESSION_FILE)
        if not folders:
            self.recent_menu.add_command(label='暂无最近项目', state='disabled')
        for value in folders:
            folder = Path(value)
            exists = (folder / 'project.json').is_file()
            title = folder.name
            if exists:
                try:
                    title = json.loads((folder / 'project.json').read_text(encoding='utf-8')).get('title') or title
                except (OSError, ValueError, AttributeError):
                    pass
            label = f'{title}  —  {folder}' + ('' if exists else '（已移动或不存在）')
            self.recent_menu.add_command(label=label, state='normal' if exists else 'disabled',
                                         command=lambda p=folder: self.open_recent_project(p))

    def open_recent_project(self, folder):
        if self.busy or self.delivery_active:
            messagebox.showinfo('任务进行中', '请等待当前任务结束后再切换项目。', parent=self)
            return
        try:
            if not (folder / 'project.json').is_file():
                raise ValueError('项目已移动或不存在，请通过“打开项目”重新选择。')
            self.set_project(folder)
        except Exception as exc:
            messagebox.showerror('打开失败', str(exc), parent=self)
        self.refresh_recent_projects()

    def restore_project(self):
        if not SESSION_FILE.exists():
            return
        try:
            value = json.loads(SESSION_FILE.read_text(encoding='utf-8'))
            folder = Path(value['folder'])
            if not (folder / 'project.json').is_file():
                self.status.set('上次项目已移动或不存在，请点击“打开项目”重新选择。')
                return
            self.set_project(folder)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.set('无法恢复上次项目，请点击“打开项目”重新选择。')

    def new_project(self):
        if self.busy or self.delivery_active:
            return
        window=self.new_popup();window.title('新建拍摄项目')
        box=ttk.Frame(window,padding=20);box.pack(fill='both',expand=True)
        title=tk.StringVar(value=time.strftime('%Y-%m-%d 拍摄'))
        location=tk.StringVar(value=str(ROOT/'projects'))
        ttk.Label(box,text='项目名称：').pack(anchor='w')
        entry=ttk.Entry(box,textvariable=title,width=60);entry.pack(fill='x',pady=(6,14))
        ttk.Label(box,text='项目存储位置：').pack(anchor='w')
        path_row=ttk.Frame(box);path_row.pack(fill='x',pady=6)
        ttk.Entry(path_row,textvariable=location,width=52).pack(side='left',fill='x',expand=True)
        def browse():
            path=filedialog.askdirectory(title='选择项目存储位置',initialdir=location.get() if Path(location.get()).is_dir() else str(ROOT),parent=window)
            if path:location.set(path)
        ttk.Button(path_row,text='浏览…',command=browse).pack(side='left',padx=(8,0))
        ttk.Label(box,text='将在所选位置下创建独立项目文件夹。导入原片仍引用原位置。',wraplength=560).pack(anchor='w',pady=8)
        def create(event=None):
            name=title.get().strip();target=location.get().strip()
            if not name or not target:
                messagebox.showerror('信息不完整','请填写项目名称和存储位置。',parent=window);return
            if self.busy or self.delivery_active:return
            from project import safe_name
            try:
                parent=Path(target).expanduser()
                if not parent.is_absolute():raise ValueError('请选择完整的绝对路径。')
                parent.mkdir(parents=True,exist_ok=True)
                folder=parent/(time.strftime('%Y%m%d_%H%M%S')+'_'+safe_name(name)+'_'+secrets.token_hex(2))
                folder.mkdir(exist_ok=False)
                self.set_project(folder,name)
            except Exception as exc:
                messagebox.showerror('创建项目失败',str(exc),parent=window);return
            window.destroy()
        ttk.Button(box,text='创建项目',command=create).pack(anchor='e',pady=(10,0))
        window.bind('<Return>',create)
        window.bind('<Escape>',lambda event:window.destroy())
        self.place_popup(window);window.grab_set();entry.focus_set()

    def open_project(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title='选择项目文件 project.json', initialdir=str(ROOT / 'projects'), filetypes=[('返图项目', '*.json')], parent=self)
        if path:
            try:
                if Path(path).name != 'project.json':
                    raise ValueError('请选择项目目录中的 project.json')
                self.set_project(Path(path).parent)
            except Exception as exc:
                messagebox.showerror('打开失败', str(exc), parent=self)

    def run(self, operation, done):
        if self.busy:
            return
        self.busy = True
        self.task_progress_text.set('正在处理…')
        self.task_progress_bar.configure(mode='indeterminate', value=0)
        self.task_progress_bar.start(15)
        def worker():
            try:
                result = operation(lambda text:self.events.put(('progress', text)))
                self.events.put(('done', (done, result)))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def pump(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    self.update_task_progress(value)
                elif kind == 'error':
                    self.busy = False
                    self.task_progress_bar.stop()
                    self.task_progress_text.set('处理失败')
                    self.status.set('任务未完成：' + value)
                    messagebox.showerror('任务未完成', value, parent=self)
                elif kind == 'history_scan':
                    self.history_scan_active = False
                    self.history_report = value
                    self.after(500, self.notify_history_report)
                else:
                    self.busy = False
                    self.task_progress_bar.stop()
                    self.task_progress_bar.configure(mode='determinate', value=100)
                    self.task_progress_text.set('处理结束')
                    done, result = value
                    done(result)
        except queue.Empty:
            pass
        self.after(100, self.pump)

    def update_task_progress(self, value):
        import re
        if isinstance(value, dict):
            text=value['text'];current=value.get('current');total=value.get('total')
        else:
            text=str(value)
            match=re.search(r'(\d+)/(\d+)',text)
            current,total=(int(match[1]),int(match[2])) if match else (None,None)
        self.status.set(text)
        if total is not None and total > 0 and current is not None:
            percent=min(100,max(0,current/total*100))
            self.task_progress_bar.stop()
            self.task_progress_bar.configure(mode='determinate',value=percent)
            self.task_progress_text.set(f'{percent:.0f}% · {current}/{total} '+(value.get('unit','项') if isinstance(value,dict) else '项'))
        else:
            if self.task_progress_bar.cget('mode') != 'indeterminate':
                self.task_progress_bar.configure(mode='indeterminate',value=0)
                self.task_progress_bar.start(15)
            self.task_progress_text.set(text[:55])

    def periodic_history_scan(self):
        if not self.history_scan_active:
            self.start_history_scan()
        self.after(300000, self.periodic_history_scan)

    def start_history_scan(self):
        if self.history_scan_active:
            self.after(3000, self.start_history_scan)
            return
        self.history_scan_active = True
        recent = read_recent(SESSION_FILE)
        current = str(self.project.folder) if self.project else None
        def worker():
            from history_scan import scan_history
            try:
                report = scan_history(ROOT/'projects', recent, current)
            except Exception as exc:
                logger.exception('HISTORY scan failed')
                report = dict(duplicates=[],errors=[str(exc)],digest='',projects=0)
            self.events.put(('history_scan',report))
        threading.Thread(target=worker,daemon=True).start()

    def notify_history_report(self):
        report = self.history_report
        if not report or not report['duplicates']:
            return
        if self.busy or self.delivery_active or self.grab_current():
            self.after(3000,self.notify_history_report)
            return
        try:
            notified = json.loads((ROOT/'.history-notice.json').read_text(encoding='utf-8')).get('digest')
        except (OSError,ValueError,AttributeError):
            notified = None
        if notified == report['digest']:return
        self.open_history_report()
        try:
            (ROOT/'.history-notice.json').write_text(json.dumps({'digest':report['digest']}),encoding='utf-8')
        except OSError:
            logger.exception('HISTORY notice could not be remembered')

    def open_history_report(self):
        report = self.history_report
        if report is None:
            messagebox.showinfo('后台检测中','历史项目正在后台检测，请稍后查看。',parent=self)
            return
        window = self.new_popup()
        window.title('历史项目重复人物检测清单')
        window.geometry('1120x600')
        frame = ttk.Frame(window,padding=16)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text=f"已读取 {report['projects']} 个项目 · {len(report['duplicates'])} 个重复账号 · {len(report['errors'])} 个项目读取失败").pack(anchor='w')
        ttk.Label(frame,text='自动检测清单：按账号核对人物和项目。当前版本仅检测，不执行文件移动。').pack(anchor='w',pady=8)
        columns=('account','person','project','count','missing','state','folder')
        table=ttk.Treeview(frame,columns=columns,show='headings',style='Small.Treeview')
        for key,label,width in zip(columns,('重复账号','人物','拍摄项目','素材数','缺失','检查结果','项目路径'),(180,100,150,60,60,130,330)):
            table.heading(key,text=label);table.column(key,width=width,stretch=key=='folder')
        scroll=ttk.Scrollbar(frame,command=table.yview)
        scroll.pack(side='right',fill='y');table.configure(yscrollcommand=scroll.set)
        table.pack(fill='both',expand=True)
        for duplicate in report['duplicates']:
            account=('QQ：' if duplicate['channel']=='qq' else '微信：')+duplicate['account']
            for entry in duplicate['entries']:
                state='另一账号冲突' if duplicate['conflict'] else '待人工核对'
                if entry['missing']:state+='；原片缺失'
                table.insert('','end',values=(account,entry['person'],entry['project'],entry['count'],entry['missing'],state,entry['folder']))
        if report['errors']:
            ttk.Button(frame,text='查看未能检测的项目',command=lambda:messagebox.showinfo('读取失败','\n'.join(report['errors']),parent=window)).pack(anchor='w',pady=8)
        self.place_popup(window)

    def import_folder(self):
        gid = self.import_target()
        if not gid:
            return
        self.choose_source_folders(lambda folders:self.import_paths(gid,folders=folders))

    def choose_source_folders(self, on_selected, initial=(), title='批量添加素材文件夹'):
        window=self.new_popup();window.title(title);window.geometry('720x560')
        box=ttk.Frame(window,padding=16);box.pack(fill='both',expand=True)
        location=tk.StringVar(value=str(Path.home()))
        chosen={str(path).casefold():str(path) for path in initial}
        hint=tk.StringVar(value=f'已选 {len(chosen)} 个目录；Ctrl / Shift 多选，双击进入文件夹。')
        top=ttk.Frame(box);top.pack(fill='x')
        ttk.Entry(top,textvariable=location).pack(side='left',fill='x',expand=True)
        table=ttk.Treeview(box,columns=('folder',),show='headings',selectmode='extended',style='Small.Treeview')
        table.heading('folder',text='文件夹（Ctrl / Shift 可多选）')
        scroll=ttk.Scrollbar(box,command=table.yview);scroll.pack(side='right',fill='y')
        table.configure(yscrollcommand=scroll.set)
        def refresh(event=None):
            try:
                root=Path(location.get()).expanduser().resolve(strict=True)
                entries=sorted([p for p in root.iterdir() if p.is_dir()],key=lambda p:p.name.casefold())
            except OSError as exc:
                messagebox.showerror('无法读取目录',str(exc),parent=window);return
            location.set(str(root));table.delete(*table.get_children())
            for index,path in enumerate(entries):table.insert('','end',iid=str(index),values=(str(path),))
        def browse():
            path=filedialog.askdirectory(title='选择这些素材文件夹的上级目录',parent=window)
            if path:location.set(path);refresh()
        ttk.Button(top,text='浏览位置…',command=browse).pack(side='left',padx=6)
        ttk.Button(top,text='上一级',command=lambda:(location.set(str(Path(location.get()).parent)),refresh())).pack(side='left')
        ttk.Button(box,text='转到填写的位置',command=refresh).pack(anchor='w',pady=6)
        table.pack(fill='both',expand=True)
        def enter(event):
            ident=table.identify_row(event.y)
            if ident:location.set(table.item(ident,'values')[0]);refresh()
        table.bind('<Double-1>',enter)
        ttk.Label(box,text='已加入的目录：').pack(anchor='w',pady=(6,0))
        selected_list=tk.Listbox(box,height=3,selectmode='extended',exportselection=False)
        selected_list.pack(fill='x')
        def update_chosen():
            selected_list.delete(0,'end')
            for path in chosen.values():selected_list.insert('end',path)
            hint.set(f'已选 {len(chosen)} 个文件夹，可切换位置继续添加。')
        def add_selected():
            for ident in table.selection():
                path=table.item(ident,'values')[0];chosen[path.casefold()]=path
            update_chosen()
        ttk.Label(box,textvariable=hint).pack(anchor='w',pady=8)
        buttons=ttk.Frame(box);buttons.pack(fill='x')
        ttk.Button(buttons,text='加入选中目录',command=add_selected).pack(side='left')
        def remove_selected():
            values=list(chosen)
            for index in selected_list.curselection():chosen.pop(values[index],None)
            table.selection_remove(*table.selection())
            update_chosen()
        ttk.Button(buttons,text='移除目录',command=remove_selected).pack(side='left',padx=6)
        ttk.Button(buttons,text='清空已选',command=lambda:(chosen.clear(),table.selection_remove(*table.selection()),update_chosen())).pack(side='left',padx=6)
        def start():
            add_selected()
            if not chosen:
                messagebox.showinfo('选择文件夹','请先选择一个或多个文件夹。',parent=window);return
            folders=list(chosen.values());window.destroy()
            on_selected(folders)
        ttk.Button(buttons,text='确认目录并开始',command=start).pack(side='right')
        update_chosen()
        self.place_popup(window)
        refresh();window.grab_set()

    def import_files(self):
        gid = self.import_target()
        if not gid:
            return
        paths = filedialog.askopenfilenames(title='添加照片和视频', filetypes=[('照片和视频', ' '.join('*'+ext for ext in sorted(PHOTOS | VIDEOS | RAW))), ('所有文件', '*.*')], parent=self)
        if paths:
            self.import_paths(gid, paths=paths)

    def import_target(self):
        if not self.require():return None
        if self.current_group == '?' and len(self.groups.curselection()) <= 1:
            return '?'
        return self.selected_group()

    def import_paths(self, gid, folder=None, paths=None, folders=None):
        project = self.project
        def operation(progress):
            if folders:
                found,skipped,seen=[],[],set()
                for index,directory in enumerate(folders,1):
                    progress(f'扫描目录 {index}/{len(folders)}：{directory}')
                    collected,ignored=collect(directory,progress=progress)
                    skipped.extend(ignored)
                    for path in collected:
                        key=str(path).casefold()
                        if key not in seen:seen.add(key);found.append(path)
                skipped=list(dict.fromkeys(skipped))
            else:
                found, skipped = collect(folder, progress=progress) if folder else ([Path(p).resolve() for p in paths], [])
            added = existing = 0
            failures = []
            for n, path in enumerate(found, 1):
                if path.suffix.lower() not in PHOTOS | VIDEOS | RAW:
                    skipped.append(str(path))
                    continue
                progress({'text':f'生成预览：{path.name}', 'current':n-1, 'total':len(found)})
                try:
                    info = fingerprint(path)
                    with project.lock:
                        prior = next((i for i in project.data['items'] if i['path'] == str(path)), None)
                    unchanged = prior and not prior['error'] and info == {'size':prior['size'], 'mtime':prior['mtime']}
                    if unchanged:
                        with project.lock:
                            if gid == '?' or (gid in prior['groups'] and prior['reviewed']):
                                existing += 1
                                continue
                            prior['groups'] = list(dict.fromkeys(prior['groups'] + [gid]))
                            prior['reviewed'] = True
                            project.unpublish()
                            project.save()
                    else:
                        from media_ids import available_id
                        ident=prior['id'] if prior else available_id(hashlib.sha256(str(path).encode()).hexdigest()[:20],str(path),{record['id'] for record in project.data['items']})
                        item = import_media(path, project.cache, gid,ident=ident)
                        if gid == '?':
                            item.update(groups=[], reviewed=False, note='未分配到人物文件夹')
                        with project.lock:
                            if prior:
                                item['groups'] = list(dict.fromkeys(prior['groups'] + ([] if gid == '?' else [gid])))
                                if gid == '?':item['reviewed'] = prior['reviewed']
                                project.data['items'][project.data['items'].index(prior)] = item
                                (project.cache / (item['id'] + '.mp4')).unlink(missing_ok=True)
                            else:
                                from media_ids import available_id
                                old_ident=item['id']
                                item['id']=available_id(old_ident,item['path'],{record['id'] for record in project.data['items']})
                                if item['id']!=old_ident:
                                    for suffix in ('.jpg','.webp','.preview-v2'):
                                        shutil.copy2(project.cache/(old_ident+suffix),project.cache/(item['id']+suffix))
                                project.data['items'].append(item)
                            project.unpublish()
                            project.save()
                    added += 1
                except Exception as exc:
                    failures.append(path.name + '：' + str(exc))
            progress({'text':'导入处理完成，正在保存素材清单…','current':len(found),'total':len(found)})
            with project.lock:
                project.data['skipped'] = skipped
                if folder or folders:
                    tracked=project.data.setdefault('source_folders',[])
                    for directory in folders or [folder]:
                        resolved=str(Path(directory).resolve())
                        prior_root=next((entry for entry in tracked if entry['path'].casefold()==resolved.casefold()),None)
                        if prior_root:
                            if prior_root.get('group') != gid:prior_root['group']='?'
                        else:tracked.append({'path':resolved,'group':gid})
                project.save()
                project.manifests()
            return added, existing, len(skipped), failures
        def done(result):
            self.current_group = gid
            self.refresh()
            added, existing, skipped, failures = result
            self.status.set(f'添加完成：新增/更新 {added} 项，已有 {existing} 项，跳过 {skipped} 项，失败 {len(failures)} 项。')
            if failures:
                messagebox.showwarning('部分素材未添加', '\n'.join(failures[:12]), parent=self)
        self.run(operation, done)

    def open_group_folder(self):
        gid = self.selected_group()
        if gid:
            os.startfile(str(self.project.group_folder(gid)))

    def refresh(self, preserve_view=False):
        if not self.project:
            return
        selected_groups = [self.group_ids[n] for n in self.groups.curselection()] if preserve_view else []
        group_scroll = self.groups.yview()[0] if preserve_view else None
        item_scroll = self.tree.yview()[0] if preserve_view else None
        data = self.project.snapshot()
        self.groups.delete(0, 'end')
        self.group_ids = ['*', '?'] + list(data['groups'])
        pending = sum(not i['groups'] for i in data['items'])
        self.groups.insert('end', f'全部素材  ({len(data["items"])})', f'未分配  ({pending})')
        for gid, group in data['groups'].items():
            count = sum(gid in i['groups'] for i in data['items'])
            contact_text = ' · 待重新绑定账号' if group.get('legacy_contacts') and not group.get('contacts') else ''
            contact_text += ''.join(f' · {"QQ" if channel == "qq" else "微信"}：{account}' for channel, account in group.get('contacts', {}).items() if account)
            self.groups.insert('end', f'{group["name"]}  ({count})' + contact_text + (' ✓ 已发布  |  ' + (data.get('public_url') or 'http://127.0.0.1:17860') + '/a/' + group['token'] + '/' if group['published'] else '  |  未发布'))
        if self.current_group not in self.group_ids:
            self.current_group = '*'
        retained = [gid for gid in selected_groups if gid in self.group_ids]
        for gid in retained or [self.current_group]:
            self.groups.selection_set(self.group_ids.index(gid))
        self.groups.activate(self.group_ids.index(self.current_group))
        self.summary.set(f'{len(data["items"])} 项素材 · {len(data["groups"])} 个人物文件夹 · {pending} 项未分配。先选择人物，再添加素材；Ctrl / Shift 可多选。')
        self.render_items(data)
        self.update_link()
        self.render_results(data)
        if preserve_view:
            self.groups.yview_moveto(group_scroll)
            self.tree.yview_moveto(item_scroll)

    def choose_group(self, event=None):
        if self.project and self.groups.curselection():
            selected = self.groups.curselection()
            active = self.groups.index('active')
            self.current_group = self.group_ids[active if active in selected else selected[0]]
            self.page = 0
            self.render_items(self.project.snapshot())
            self.update_link()

    def select_all_people(self, event=None):
        self.groups.selection_set(0, 'end')
        self.choose_group()
        return 'break'

    def select_all_media(self, event=None):
        self.tree.selection_set(self.tree.get_children())
        self.select_item()
        self.status.set('已全选当前页素材。Ctrl 点击逐项选择，Shift 点击连续选择。')
        return 'break'

    def person_at(self, x, y):
        if not (0 <= x < self.groups.winfo_width() and 0 <= y < self.groups.winfo_height()):
            return None
        index = self.groups.nearest(y)
        box = self.groups.bbox(index)
        if not box or not box[1] <= y < box[1] + box[3]:
            return None
        return index if index < len(self.group_ids) else None

    def person_menu(self, event):
        if not self.require():
            return 'break'
        index = self.person_at(event.x, event.y)
        self.person_context.delete(0, 'end')
        self.person_context.add_command(label='新建人物文件夹', command=self.new_group)
        if index is not None and self.group_ids[index] in self.project.data['groups']:
            gid = self.group_ids[index]
            if index not in self.groups.curselection():
                self.groups.selection_clear(0, 'end')
                self.groups.selection_set(index)
            self.groups.activate(index)
            self.choose_group()
            self.person_context.add_separator()
            gids = [self.group_ids[i] for i in self.groups.curselection() if self.group_ids[i] in self.project.data['groups']]
            if len(gids) == 1:
                self.person_context.add_command(label='重命名人物', command=self.rename_group)
            self.person_context.add_command(label=f'删除选中人物文件夹（{len(gids)}）', command=lambda:self.delete_people(gids))
        try:
            self.person_context.tk_popup(event.x_root, event.y_root)
        finally:
            self.person_context.grab_release()
        return 'break'

    def delete_person(self, gid):
        self.delete_people([gid])

    def delete_people(self, gids):
        if not self.require():
            return
        for gid in gids:
            self.project.delete_group(gid)
        self.current_group = '?'
        self.page = 0
        self.refresh()
        self.status.set(f'已删除 {len(gids)} 个人物文件夹。独属素材转为未分配；原片和历史返图包保留，项目状态已备份。')

    def begin_drag(self, event):
        self.clear_drag()
        self.tree.focus_set()
        if not self.project or self.busy:
            return
        row = self.tree.identify_row(event.y, event.x)
        if not row:
            return
        self.drag_start = (event.x_root, event.y_root, row)
        # Preserve a multiple selection when dragging an already selected row.
        if row in self.tree.selection() and not event.state & 0x0005:
            self.tree.focus(row)
            return 'break'

    def drag_motion(self, event):
        if not self.drag_start:
            return
        x, y, row = self.drag_start
        if not self.drag_active and abs(event.x_root-x) + abs(event.y_root-y) < 8:
            return
        if not self.drag_active:
            self.drag_ids = set(self.tree.selection()) or {row}
            self.drag_ids = {ident for ident in self.drag_ids if not ident.startswith('archive:')}
            if not self.drag_ids:
                return
            self.drag_active = True
            self.tree.configure(cursor='fleur')
        index = self.person_at(event.x_root-self.groups.winfo_rootx(), event.y_root-self.groups.winfo_rooty())
        if index is not None and self.group_ids[index] not in self.project.data['groups']:
            index = None
        if self.drag_hover != index:
            if self.drag_hover is not None:
                self.groups.itemconfigure(self.drag_hover, background='white')
            self.drag_hover = index
            if index is not None:
                self.groups.itemconfigure(index, background='#cce5dc')
        self.status.set(f'正在拖动 {len(self.drag_ids)} 项素材；松开鼠标将原片移动到人物文件夹。')
        return 'break'

    def clear_drag(self):
        if self.drag_hover is not None and self.drag_hover < self.groups.size():
            self.groups.itemconfigure(self.drag_hover, background='white')
        self.tree.configure(cursor='')
        self.drag_start = None
        self.drag_ids = set()
        self.drag_hover = None
        self.drag_active = False

    def end_drag(self, event):
        if not self.drag_active:
            if self.drag_start and not event.state & 0x0005:
                self.tree.selection_set(self.drag_start[2])
                self.select_item()
            self.clear_drag()
            return
        ids = self.drag_ids.copy()
        index = self.person_at(event.x_root-self.groups.winfo_rootx(), event.y_root-self.groups.winfo_rooty())
        gid = self.group_ids[index] if index is not None else None
        self.clear_drag()
        if gid in self.project.data['groups']:
            project = self.project
            def operation(progress):
                try:
                    return project.move_items(ids, gid, move_originals=True, progress=progress), ''
                except Exception as exc:
                    return 0, str(exc)
                finally:
                    project.manifests()
            def done(result):
                self.refresh(preserve_view=True)
                count, error = result
                if error:
                    self.status.set('移动未全部完成：' + error)
                    messagebox.showerror('原片移动未全部完成', error + '\n已完成的移动保留，请检查列表中的原片路径。', parent=self)
                else:
                    self.status.set(f'{count} 项原片已移动到人物文件夹“{project.data["groups"][gid]["name"]}”的原片目录。请重新发布相册。')
            self.run(operation, done)
        else:
            self.status.set('拖动已取消。请将素材拖到具体人物文件夹，不能放到“全部素材”或“未分配”。')
        return 'break'

    def render_items(self, data):
        chosen = self.current_group
        selected_groups = {self.group_ids[n] for n in self.groups.curselection()}
        if not selected_groups:
            selected_groups = {chosen}
        items = [i for i in data['items'] if '*' in selected_groups or
                 ('?' in selected_groups and not i['groups']) or selected_groups.intersection(i['groups'])]
        view_key = (str(self.project.folder), tuple(sorted(selected_groups)))
        previous_key = getattr(self, '_media_view_key', None)
        positions = getattr(self, '_media_scroll_positions', {})
        if previous_key is not None:
            positions[previous_key] = self.tree.scroll_position()
        self._media_scroll_positions = positions
        self._media_view_key = view_key
        self.page_info.set(f'当前文件夹 {len(items)} 项 · 滚动查看全部素材')
        self.tree.delete(*self.tree.get_children())
        self._archive_previews = {}
        self.images = {}
        for item in items:
            path = self.project.cache / (item['id'] + '.webp')
            group_text = '、'.join(data['groups'][g]['name'] for g in item['groups'] if g in data['groups']) or '未归类'
            state = '该文件已删除' if item.get('source_missing') else ('读取失败' if item['error'] else ('原片已移动到人物文件夹' if item.get('managed_original') else ('已归入人物文件夹' if item['groups'] else '未分配到人物文件夹')))
            name = ('▶ ' if item['kind'] == 'video' else '') + item['name']
            self.tree.insert('', 'end', iid=item['id'], path=path, values=(name, group_text, state), returned=item.get('returned_to_coser',False))
        for gid, group in data['groups'].items():
            if '*' not in selected_groups and gid not in selected_groups:
                continue
            folder = self.project.folder / '人物' / ('人物_' + gid)
            for archive in sorted(folder.glob('*.zip')):
                ident = 'archive:' + str(archive)
                self._archive_previews[ident] = archive
                self.tree.insert('', 'end', iid=ident, values=(archive.name, group['name'], '返图压缩包'))
        for record in data.get('manual_archives',[]):
            archive=Path(record['path']);ident='archive:'+str(archive)
            related=set(record.get('groups',[]))
            if ident in self._archive_previews or not archive.is_file():continue
            if '*' not in selected_groups and not selected_groups.intersection(related) and not ('?' in selected_groups and not related):continue
            self._archive_previews[ident]=archive
            self.tree.insert('','end',iid=ident,values=(archive.name,record.get('person','未分配'),'返图压缩包'))
        self.tree.restore_scroll(positions.get(view_key, 0))

    def select_item(self, event=None):
        selected = set(self.tree.selection())
        if not selected or not self.project:
            return
        archive = next((self._archive_previews[i] for i in selected if i in self._archive_previews), None)
        if archive:
            self.detail.set('返图压缩包：' + str(archive) + ' · 双击打开')
            return
        item = next(i for i in self.project.data['items'] if i['id'] in selected)
        names = '、'.join(self.project.data['groups'][gid]['name'] for gid in item['groups'] if gid in self.project.data['groups'])
        archive = (f'原片已移动到人物文件夹：{names}。' if item.get('managed_original') else f'已归入人物文件夹：{names}。原片保留在原位置。') if names else '尚未分配到人物文件夹。'
        self.detail.set(f'已选 {len(selected)} 项 · 原片：{item["path"]}\n{item["error"] or archive}')

    def check_duplicate_media(self):
        if not self.require():return
        if self.delivery_active:
            messagebox.showinfo('正在发送','请等待当前发送结束后检测重复。',parent=self);return
        project=self.project
        selected={self.group_ids[index] for index in self.groups.curselection()} or {self.current_group}
        scope=None if selected & {'*','?'} else selected & project.data['groups'].keys()
        if scope is not None and not scope:
            messagebox.showinfo('选择检测范围','请选择人物、全部素材或未分配。',parent=self);return
        label='当前工程全部素材' if scope is None else '、'.join(project.data['groups'][gid]['name'] for gid in sorted(scope))
        from duplicate_media import deduplicate_project,find_similar,refresh_duplicate_previews
        def operation(progress):
            refresh_duplicate_previews(project,scope,progress)
            count=deduplicate_project(project,progress,scope)
            rows=find_similar(project.snapshot()['items'],scope,progress)
            return count,rows
        def done(result):
            count,rows=result
            self.refresh(preserve_view=True)
            if count:
                self.refresh(preserve_view=True)
                self.status.set(f'{label}：已合并 {count} 项内容相同的重复记录，人物归属和原片保留。')
            else:
                self.status.set('检测完成，没有可合并的完全相同图片。')
            if rows:self.show_duplicate_media(rows)
            else:messagebox.showinfo('重复检测完成',f'已合并 {count} 项完全相同的素材记录。\n磁盘原片保留。' if count else '未发现完全相同或符合阈值的相似素材。',parent=self)
        self.status.set('检测重复：'+label)
        self.run(operation,done)

    def show_duplicate_media(self, rows):
        if not rows:
            messagebox.showinfo('检测重复','没有发现已分配与未分配之间的同名素材。',parent=self);return
        window=self.new_popup();window.title('核对重复素材');window.geometry('1150x580')
        box=ttk.Frame(window,padding=16);box.pack(fill='both',expand=True)
        ttk.Label(box,text='双击列表左右对比。确认合并后保留右侧原片，删除左侧源文件及预览，合并人物归属和返图标记。').pack(anchor='w')
        table=ttk.Treeview(box,columns=('name','state','person','old','target'),show='headings',style='Small.Treeview')
        for key,label,width in [('name','文件名',140),('state','检查结果',200),('person','人物',130),('old','待合并路径',300),('target','保留路径',300)]:
            table.heading(key,text=label);table.column(key,width=width)
        scroll=ttk.Scrollbar(box,command=table.yview);scroll.pack(side='right',fill='y')
        table.configure(yscrollcommand=scroll.set);table.pack(fill='both',expand=True,pady=10)
        for index,row in enumerate(rows):
            person='、'.join(self.project.data['groups'].get(gid,{}).get('name',gid) for gid in row['groups'])
            table.insert('','end',iid=str(index),values=(row['name'],row['state'],person,row['old'],row['target']))
        table.selection_set([str(index) for index,row in enumerate(rows) if row['state']=='文件内容完全相同'])
        def apply():
            if not self.require():return
            chosen=[rows[int(index)] for index in table.selection()]
            if not chosen:return
            count=len({row['remove'] for row in chosen})
            if not messagebox.askyesno('确认合并并删除',f'合并 {count} 项素材记录，并删除对应左侧源文件和预览？\n保留右侧文件。相似画面可能是不同连拍，请先双击核对。\n源文件删除不可通过项目备份恢复。',parent=window):return
            from duplicate_media import merge_reviewed
            try:removed=merge_reviewed(self.project,chosen,delete_originals=True)
            except Exception as exc:messagebox.showerror('无法清理',str(exc),parent=window);return
            window.destroy();self.refresh(preserve_view=True)
            self.status.set(f'已合并 {removed} 项重复记录并删除多余原片及预览。')
        ttk.Button(box,text='确认合并并删除选中重复文件',command=apply).pack(anchor='e')
        def compare(event):
            ident=table.identify_row(event.y)
            if not ident:return
            row=rows[int(ident)]
            project=self.project
            popup=self.new_popup();popup.title('左右对比：确认后保留右侧并删除左侧');popup.geometry('1200x800')
            body=ttk.Frame(popup,padding=12);body.pack(fill='both',expand=True)
            pictures=ttk.Frame(body);pictures.pack(fill='both',expand=True)
            from image_preview import ZoomPreview
            for side,path in [('待删除（左侧）',row['old']),('保留（右侧）',row['target'])]:
                column=ttk.Frame(pictures);column.pack(side='left',fill='both',expand=True,padx=6)
                ttk.Label(column,text=side).pack(anchor='w')
                try:
                    with Image.open(path) as image:view=ZoomPreview(column,image)
                except (OSError,ValueError) as exc:
                    ttk.Label(column,text='无法读取原片：'+str(exc),wraplength=500).pack();continue
                view.pack(fill='both',expand=True)
                ttk.Label(column,text=str(path),wraplength=500).pack(pady=6)
            def confirm():
                if self.busy or self.delivery_active:
                    messagebox.showinfo('任务进行中','请等待当前任务结束。',parent=popup);return
                if not messagebox.askyesno('确认合并并删除',f'保留右侧：\n{row["target"]}\n\n删除左侧源文件及其预览：\n{row["old"]}\n\n人物归属和返图标记合并到右侧。原片删除不可通过项目备份恢复。',parent=popup):return
                from duplicate_media import merge_reviewed
                try:merge_reviewed(project,[row],delete_originals=True)
                except Exception as exc:
                    messagebox.showerror('合并结果',str(exc),parent=popup);self.refresh(preserve_view=True);return
                removed=row['remove'];popup.destroy();window.grab_set()
                for key in table.get_children():
                    candidate=rows[int(key)]
                    if removed in (candidate['remove'],candidate['keep']):table.delete(key)
                self.refresh(preserve_view=True)
                self.status.set('已确认合并：保留右侧原片，删除左侧重复源文件及预览。')
            ttk.Button(body,text='确认合并图片（保留右侧，删除左侧）',command=confirm).pack(pady=8)
            def close_compare():popup.destroy();window.grab_set()
            popup.protocol('WM_DELETE_WINDOW',close_compare)
            self.place_popup(popup);popup.grab_set()
        table.bind('<Double-1>',compare)
        self.place_popup(window);window.grab_set()

    def refresh_sources(self):
        if not self.require():return
        if self.delivery_active:
            messagebox.showinfo('正在发送','请等待发送结束后刷新素材。',parent=self);return
        project=self.project
        existing=copy.deepcopy(project.data.get('source_folders',[]))
        gid=self.current_group if self.current_group in project.data['groups'] else '?'
        def selected(folders):
            if self.busy or self.delivery_active:
                messagebox.showinfo('任务进行中','请等待当前任务结束后刷新。',parent=self);return
            old={entry['path'].casefold():entry for entry in existing}
            roots=[old.get(str(Path(folder).resolve()).casefold(),{'path':str(Path(folder).resolve()),'group':gid}) for folder in folders]
            with project.lock:
                project.data['source_folders']=roots;project.save()
            self.refresh_selected_sources(roots)
        self.choose_source_folders(selected,initial=[entry['path'] for entry in existing],title='选择需要刷新的素材文件夹（可多选）')

    def refresh_selected_sources(self, roots):
        project=self.project
        def operation(progress):
            from sync_media import scan_folder,apply_sync
            snapshot=project.snapshot()
            changes={}
            # More specific imported subfolders take precedence for new files.
            for entry in sorted(roots,key=lambda entry:len(Path(entry['path']).parts)):
                path=Path(entry['path'])
                if not path.is_dir():
                    raise ValueError('导入目录不可访问，未移除任何记录。请检查硬盘或使用“重新定位原片”：\n'+str(path))
                progress('刷新目录：'+str(path))
                for row in scan_folder(snapshot['items'],path,progress,snapshot.get('ignored_source_paths',[])):
                    row['group']=entry.get('group','?')
                    changes[(row['id'] or row['path'].casefold())]=row
            rows=list(changes.values())
            if rows:apply_sync(project,rows,'?',progress)
            return {action:sum(row['action']==action for row in rows) for action in ('新增','缺失','内容变化')}
        def done(counts):
            self.refresh(preserve_view=True)
            self.status.set(f"刷新完成：新增 {counts['新增']} 项，标记已删除 {counts['缺失']} 项，更新 {counts['内容变化']} 项。"+
                            ('请重新发布相册并核对发送任务。' if any(counts.values()) else '素材没有变化。'))
        self.run(operation,done)

    def sync_source_folder(self):
        if not self.require():return
        gid=self.selected_group()
        if not gid:return
        if self.delivery_active:
            messagebox.showinfo('正在发送','请等待当前发送结束后同步素材。',parent=self);return
        folder=filedialog.askdirectory(title='选择需要同步的原片文件夹（包含子文件夹）',parent=self)
        if not folder:return
        from sync_media import scan_folder
        items=copy.deepcopy(self.project.data['items'])
        self.run(lambda progress:scan_folder(items,folder,progress),lambda rows:self.show_sync_review(rows,gid,folder))

    def show_sync_review(self, rows, gid, folder):
        if not rows:
            messagebox.showinfo('同步检查','所选目录没有新增、缺失或变化的素材。',parent=self);return
        window=self.new_popup();window.title('同步素材文件夹：确认变更');window.geometry('1000x560')
        box=ttk.Frame(window,padding=16);box.pack(fill='both',expand=True)
        person=self.project.data['groups'][gid]['name']
        ttk.Label(box,text=f'新增素材归入“{person}”；已有归属保留。缺失项保留预览，标记“该文件已删除”。').pack(anchor='w')
        ttk.Label(box,text='先检查目录和硬盘是否正确。新增项不自动加入已有发送任务，请同步后重新创建任务。').pack(anchor='w',pady=6)
        table=ttk.Treeview(box,columns=('type','name','path'),show='headings',style='Small.Treeview')
        for key,label,width in [('type','变化类型',110),('name','文件名',210),('path','文件路径',610)]:
            table.heading(key,text=label);table.column(key,width=width)
        scroll=ttk.Scrollbar(box,command=table.yview);scroll.pack(side='right',fill='y')
        table.configure(yscrollcommand=scroll.set);table.pack(fill='both',expand=True)
        for index,row in enumerate(rows):table.insert('','end',iid=str(index),values=(row['action'],row['name'],row['path']))
        table.selection_set([str(index) for index,row in enumerate(rows) if row['action']!='缺失'])
        def apply():
            if self.busy or self.delivery_active:
                messagebox.showinfo('任务进行中','请等待任务结束后同步。',parent=window);return
            chosen=[rows[int(index)] for index in table.selection()]
            if not chosen:return
            if not messagebox.askyesno('确认同步',f'应用选中的 {len(chosen)} 项变更？\n项目将备份，已发布相册及发送队列暂停。\n缺失原片保留预览并标记已删除，关联任务需恢复原片后继续。',parent=window):return
            project=self.project
            with project.lock:project.data['delivery_paused']=True;project.save()
            window.destroy()
            from sync_media import apply_sync
            def done(count):
                self.refresh(preserve_view=True)
                self.status.set(f'已同步 {count} 项变更，请重新发布相册并核对发送任务。')
            self.run(lambda progress:apply_sync(project,chosen,gid,progress),done)
        ttk.Button(box,text='确认同步选中项（Ctrl / Shift 多选）',command=apply).pack(anchor='e',pady=10)
        self.place_popup(window)
        window.grab_set()

    def relocate_sources(self):
        if not self.require():return
        if self.delivery_active:
            messagebox.showinfo('正在发送','请等待当前发送结束后重新定位原片。',parent=self)
            return
        folder=filedialog.askdirectory(title='选择原片的新目录（包含子文件夹）',parent=self)
        if not folder:return
        from relocate_media import find_relocations
        items=copy.deepcopy(self.project.data['items'])
        cancel=threading.Event()
        window=self.new_popup();window.title('正在扫描原片位置')
        box=ttk.Frame(window,padding=20);box.pack(fill='both',expand=True)
        ttk.Label(box,text='后台扫描中，可以取消。请选择具体原片目录，避免扫描整个磁盘。').pack(anchor='w')
        ttk.Label(box,textvariable=self.status,wraplength=650).pack(fill='x',pady=12)
        bar=ttk.Progressbar(box,mode='indeterminate');bar.pack(fill='x');bar.start()
        def stop():
            cancel.set()
            self.status.set('正在取消扫描，等待当前文件系统操作返回…')
        ttk.Button(box,text='取消扫描',command=stop).pack(pady=(12,0))
        window.protocol('WM_DELETE_WINDOW',stop)
        self.place_popup(window)
        window.grab_set()
        self.status.set('正在检查原片位置…')
        def operation(progress):
            logger.info('RELOCATE scan start folder=%s items=%s',folder,len(items))
            try:
                rows=find_relocations(items,folder,progress,cancel)
                logger.info('RELOCATE scan complete matches=%s',len(rows))
                return rows,None
            except Exception as exc:
                logger.exception('RELOCATE scan ended')
                return None,exc
        def done(result):
            bar.stop();window.destroy()
            rows,error=result
            if isinstance(error,InterruptedError):
                self.status.set(str(error));return
            if error:
                self.status.set('重新定位扫描失败：'+str(error))
                messagebox.showerror('扫描失败',str(error),parent=self);return
            self.status.set('原片位置扫描完成。')
            self.show_relocation_review(rows)
        self.run(operation,done)

    def show_relocation_review(self, rows):
        if not rows:
            messagebox.showinfo('原片位置正常','当前项目没有缺失或发生变化的原片。',parent=self)
            return
        window=self.new_popup()
        window.title('重新定位原片：核对新路径')
        window.geometry('1100x580')
        frame=ttk.Frame(window,padding=16);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='Ctrl / Shift 多选需要更新的素材。大小和时间一致不等于内容校验；同名候选需手动指定。').pack(anchor='w')
        table=ttk.Treeview(frame,columns=('name','state','old','new'),show='headings',style='Small.Treeview')
        for key,label,width in [('name','文件名',160),('state','匹配情况',220),('old','旧路径',300),('new','新路径',300)]:
            table.heading(key,text=label);table.column(key,width=width)
        scrollbar=ttk.Scrollbar(frame,command=table.yview);scrollbar.pack(side='right',fill='y')
        table.configure(yscrollcommand=scrollbar.set);table.pack(fill='both',expand=True,pady=10)
        by_id={row['id']:row for row in rows}
        for row in rows:table.insert('','end',iid=row['id'],values=(row['name'],row['state'],row['old'],row['path']))
        table.selection_set([row['id'] for row in rows if row['path']])
        def specify():
            ids=table.selection()
            if len(ids)!=1:
                messagebox.showinfo('指定文件','请选择一项素材。',parent=window);return
            path=filedialog.askopenfilename(title='选择此素材的新原片',parent=window)
            if path:
                row=by_id[ids[0]];row.update(path=path,state='手动指定，请核对')
                table.item(ids[0],values=(row['name'],row['state'],row['old'],path))
        def apply():
            if self.busy or self.delivery_active:
                messagebox.showinfo('任务进行中','请等待当前任务结束后再更新原片。',parent=window)
                return
            chosen=[by_id[ident] for ident in table.selection()]
            if not chosen or any(not row['path'] for row in chosen):
                messagebox.showinfo('新路径不完整','请选择素材并为每项指定新路径。',parent=window);return
            if not messagebox.askyesno('确认更新原片',f'更新 {len(chosen)} 项原片路径和预览？\n人物归属保留，未发送任务同步更新。\n已发布相册暂停，更新后需重新发布；发送队列暂停。',parent=window):return
            from relocate_media import apply_relocations
            project=self.project
            window.destroy()
            with project.lock:
                project.data['delivery_paused']=True;project.save()
            def done(count):
                self.refresh(preserve_view=True)
                self.status.set(f'已重新定位 {count} 项原片；请重新发布相册，核对任务后继续发送。')
            self.run(lambda progress:apply_relocations(project,chosen,progress),done)
        buttons=ttk.Frame(frame);buttons.pack(fill='x')
        ttk.Button(buttons,text='为选中项指定文件',command=specify).pack(side='left')
        ttk.Button(buttons,text='确认更新选中素材',command=apply).pack(side='right')
        self.place_popup(window)
        window.grab_set()

    def media_context_menu(self, event):
        if not self.project:
            return
        ident = self.tree.identify_row(event.y, event.x)
        if not ident:
            return
        if ident not in self.tree.selection():
            self.tree.selection_set([ident])
        self.tree.focus(ident)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label='打开源文件位置', command=lambda:self.open_source_location(ident))
        if not ident.startswith('archive:'):
            menu.add_separator()
            can_compress=self.current_group in self.project.data['groups'] and len(self.groups.curselection())<=1
            menu.add_command(label='压缩已选中图片',command=self.compress_selected_photos,state='normal' if can_compress else 'disabled')
            ids=set(self.tree.selection())
            items=[item for item in self.project.data['items'] if item['id'] in ids]
            returned=tk.BooleanVar(value=bool(items) and all(item.get('returned_to_coser',False) for item in items))
            menu.add_checkbutton(label='已返图给 coser 老师',variable=returned,
                                 command=lambda:self.mark_returned(ids,returned.get()))
            menu.add_command(label='删除源文件和预览图…', command=self.delete_selected_sources)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return 'break'

    def compress_selected_photos(self):
        if not self.require():return
        ids=set(self.tree.selection())
        if not any(item['id'] in ids and item.get('kind')=='photo' for item in self.project.data['items']):
            messagebox.showinfo('选择图片','请先选中一张或多张图片。',parent=self);return
        project=self.project
        group=self.current_group if self.current_group in project.data['groups'] and len(self.groups.curselection())<=1 else None
        if group is None:
            messagebox.showinfo('选择人物文件夹','全部素材和未分配不支持压缩，请先进入单个人物文件夹。',parent=self);return
        from selected_archive import compress_selected
        def done(archive):
            self.render_items(project.snapshot())
            self.status.set('压缩完成：'+str(archive))
        self.run(lambda progress:compress_selected(project,ids,group,progress),done)

    def mark_returned(self, ids, value):
        if not self.require():return
        selected_tasks=self.results.selection()
        selected_media=self.tree.selection()
        with self.project.lock:
            count=0
            for item in self.project.data['items']:
                if item['id'] in ids:
                    item['returned_to_coser']=bool(value)
                    count+=1
            self.project.save()
        self.render_items(self.project.snapshot())
        self.tree.selection_set(selected_media)
        self.render_results(self.project.snapshot())
        self.results.selection_set(selected_tasks)
        self.status.set(f'已将 {count} 项素材标记为'+('已返图。' if value else '未返图。'))

    def task_return_menu(self, event):
        if not self.project:return
        ident=self.results.identify_row(event.y)
        if not ident:return
        if ident not in self.results.selection():self.results.selection_set([ident])
        selected=set(self.results.selection())
        ids=set()
        for record in self.project.data['submissions']:
            if record['id'] in selected:
                ids.update(record.get('items',[]))
                ids.update(file['id'] for file in record.get('files',[]))
        items=[item for item in self.project.data['items'] if item['id'] in ids]
        value=tk.BooleanVar(value=bool(items) and all(item.get('returned_to_coser',False) for item in items))
        menu=tk.Menu(self,tearoff=False)
        menu.add_checkbutton(label='已返图给 coser 老师',variable=value,state='normal' if items else 'disabled',
                             command=lambda:self.mark_returned(ids,value.get()))
        try:menu.tk_popup(event.x_root,event.y_root)
        finally:menu.grab_release()
        return 'break'

    def delete_selected_sources(self):
        if not self.require():return
        if self.delivery_active:
            messagebox.showinfo('正在发送','请等待发送结束后删除文件。',parent=self);return
        ids=set(self.tree.selection())
        items=[item for item in self.project.data['items'] if item['id'] in ids]
        if not items:return
        paths=list(dict.fromkeys(item['path'] for item in items))
        listing='\n'.join(paths[:12])
        if len(paths)>12:listing+='\n…'
        if not messagebox.askyesno('删除源文件和预览图',f'永久删除以下 {len(paths)} 个源文件及预览图？\n\n{listing}\n\n所有人物下对这些文件的引用都会移除。此操作无法通过项目备份恢复原片。',parent=self):return
        project=self.project
        from delete_media import delete_sources
        def done(result):
            count,errors=result
            self.refresh(preserve_view=True)
            self.status.set(f'已删除 {count} 个源文件及对应预览；请核对发送任务，重新发布相册。')
            if errors:messagebox.showwarning('部分文件未清理','\n'.join(errors),parent=self)
        self.run(lambda progress:delete_sources(project,ids,progress),done)

    def open_source_location(self, ident):
        if not self.project:
            return
        path = getattr(self, '_archive_previews', {}).get(ident)
        if path is None:
            item = next((item for item in self.project.data['items'] if item['id'] == ident), None)
            if not item:
                return
            path = Path(item['path'])
        path = Path(path)
        if not path.is_absolute():
            path = self.project.folder / path
        path = path.resolve()
        try:
            if not path.is_file():
                raise ValueError('源文件已移动或不存在：\n' + str(path))
            subprocess.Popen(['explorer.exe', '/select,', str(path)])
        except (OSError, ValueError) as exc:
            messagebox.showerror('无法打开源文件位置', str(exc), parent=self)

    def preview_item(self, event=None):
        if not self.project or not self.tree.selection():
            return
        ident = self.tree.focus() if self.tree.focus() in self.tree.selection() else self.tree.selection()[0]
        if ident in self._archive_previews:
            os.startfile(str(self._archive_previews[ident]))
            return
        path = self.project.cache / (ident + '.jpg')
        if not path.exists():
            return
        window = self.new_popup()
        window.title('素材预览')
        window.geometry('940x760')
        from image_preview import ZoomPreview, preview_photo_ids, PreviewFullscreen
        with Image.open(path) as source:
            viewer=ZoomPreview(window,source)
        viewer.pack(fill='both',expand=True)
        ttk.Label(window,text='← / → 切图 · 滚轮缩放 · 左键拖动 · 双击切换全屏 · Esc 退出全屏 · R 适合窗口').pack(pady=(6,0))
        item = next(i for i in self.project.data['items'] if i['id'] == ident)
        project=self.project
        items={item['id']:dict(item) for item in project.data['items']}
        visible=preview_photo_ids(self.tree.get_children(),project.data['items']) if item['kind']=='photo' else [ident]
        visible=[key for key in visible if (project.cache/(key+'.jpg')).is_file()]
        position=visible.index(ident)
        caption=tk.StringVar(value=f'{item["name"]} · {position+1}/{len(visible)}')
        ttk.Label(window,textvariable=caption).pack(pady=8)
        def navigate(delta):
            nonlocal position
            next_position=position+delta
            while 0 <= next_position < len(visible):
                key=visible[next_position]
                try:
                    with Image.open(project.cache/(key+'.jpg')) as source:
                        viewer.set_image(source)
                except (OSError,ValueError):
                    next_position+=delta
                    continue
                position=next_position
                caption.set(f'{items[key]["name"]} · {position+1}/{len(visible)}')
                break
            return 'break'
        window.bind('<Left>',lambda event:navigate(-1))
        window.bind('<Right>',lambda event:navigate(1))
        fullscreen=PreviewFullscreen(window)
        def set_fullscreen(enabled):
            fullscreen.set(enabled)
            viewer.drag=None
            viewer.fitted=True
            window.after_idle(viewer.fit)
            viewer.focus_set()
            return 'break'
        window.bind('<Double-1>',lambda event:set_fullscreen(not fullscreen.enabled))
        window.bind('<Escape>',lambda event:set_fullscreen(False))
        window.bind('<r>',lambda event:viewer.fit())
        window.bind('<R>',lambda event:viewer.fit())
        if item['kind'] == 'video':
            ttk.Button(window, text='用本机播放器打开原视频', command=lambda:os.startfile(item['path'])).pack(pady=8)
        self.place_popup(window)
        viewer.focus_set()

    def new_group(self):
        if not self.require():
            return
        name = self.ask_name('新建人物文件夹', '人物名称：',
                             validator=lambda name:person_name_error(self.project.data['groups'], name))
        if name and name.strip():
            gid = secrets.token_hex(6)
            with self.project.lock:
                self.project.data['groups'][gid] = {'name': name.strip()[:80], 'token': secrets.token_urlsafe(24), 'published': False}
                self.project.save()
                self.project.manifests()
            self.current_group = gid
            self.refresh()

    def selected_group(self):
        if not self.require():
            return None
        if len(self.groups.curselection()) > 1:
            messagebox.showinfo('选择一个人物', '当前选中了多个人物。添加素材、重命名或发布时，请先单击选择一个人物文件夹。', parent=self)
            return None
        if self.current_group not in self.project.data['groups']:
            messagebox.showinfo('选择人物', '请先在“人物与素材”页选择人物文件夹；没有人物时先点击“新建人物文件夹”。', parent=self)
            return None
        return self.current_group

    def rename_group(self):
        gid = self.selected_group()
        if not gid:
            return
        name = self.ask_name('修改人物名', '新名称：', initialvalue=self.project.data['groups'][gid]['name'],
                             validator=lambda name:person_name_error(self.project.data['groups'], name, exclude=gid))
        if name and name.strip():
            with self.project.lock:
                self.project.data['groups'][gid]['name'] = name.strip()[:80]
                self.project.save()
                self.project.manifests()
            self.refresh()

    def select_groups_dialog(self, title, callback, exclude=None):
        window = self.new_popup()
        window.title(title)
        window.geometry('400x420')

        ttk.Label(window, text='可选择多个人物（多人照片 / 视频）', padding=15).pack()
        listing = tk.Listbox(window, selectmode='multiple', font=('Microsoft YaHei UI', 12), exportselection=False)
        listing.pack(fill='both', expand=True, padx=15)
        gids = [g for g in self.project.data['groups'] if g != exclude]
        for gid in gids:
            listing.insert('end', self.project.data['groups'][gid]['name'])
        def apply():
            result = [gids[i] for i in listing.curselection()]
            if result:
                window.destroy()
                callback(result)
        ttk.Button(window, text='应用', command=apply).pack(pady=15)
        self.place_popup(window)
        window.grab_set()

    def assign_items(self):
        if not self.require():
            return
        ids = set(self.tree.selection())
        if not ids:
            messagebox.showinfo('选择素材', '请先选择一项或多项素材。', parent=self)
            return
        if not self.project.data['groups']:
            self.new_group()
            return
        def apply(gids):
            with self.project.lock:
                self.project.unpublish()
                for item in self.project.data['items']:
                    if item['id'] in ids and not item['error']:
                        item['groups'] = gids
                        item['reviewed'] = True
                        item['note'] = '已手动指定人物'
                self.project.save()
                self.project.manifests()
            self.refresh()
        self.select_groups_dialog('为选中素材指定人物（替换原归属）', apply)

    def remove_items(self):
        if not self.require():
            return
        ids = set(self.tree.selection())
        if not ids:
            return
        with self.project.lock:
            self.project.unpublish()
            self.project.data['items'] = [i for i in self.project.data['items'] if i['id'] not in ids]
            self.project.save()
            self.project.manifests()
        self.refresh()
        self.status.set('已移出项目，原文件保持不变。')

    def copy_originals(self):
        gid = self.selected_group()
        if not gid:
            return
        project = self.project
        data = project.snapshot()
        def operation(progress):
            count = 0
            for item in data['items']:
                if not item['reviewed'] or item['error']:
                    continue
                for target in ([gid] if gid in item['groups'] else []):
                    folder = project.group_folder(target) / '原片'
                    folder.mkdir(exist_ok=True)
                    source = Path(item['path'])
                    if fingerprint(source) != {'size': item['size'], 'mtime': item['mtime']}:
                        raise ValueError('原片已改变：' + item['name'])
                    destination = folder / (item['id'] + '_' + item['name'])
                    if destination.exists() and destination.stat().st_size == item['size']:
                        continue
                    progress('复制原片：' + item['name'])
                    temp = destination.with_suffix(destination.suffix + '.part')
                    try:
                        shutil.copy2(source, temp)
                        temp.replace(destination)
                    finally:
                        temp.unlink(missing_ok=True)
                    count += 1
            project.manifests()
            return count
        self.run(operation, lambda count:self.status.set(f'已复制 {count} 份原片。人物文件夹位于项目目录的“人物”中。'))

    def ensure_server(self):
        if not self.server:
            try:
                self.server = AlbumServer(self.project)
            except OSError as exc:
                messagebox.showerror('选片服务未启动', f'17860 端口无法使用，请关闭其他返图软件实例后重试。\n{exc}', parent=self)
                return False
        return True

    def configure_openfrp(self):
        if self.openfrp.running:
            messagebox.showinfo('OpenFrp 正在运行', '请先停止 OpenFrp 再修改配置。', parent=self)
            return
        window = tk.Toplevel(self)
        window.withdraw()
        window.title('配置 OpenFrp')
        window.transient(self)
        box = ttk.Frame(window, padding=18)
        box.pack(fill='both', expand=True)
        values = {key:tk.StringVar(value=self.openfrp.settings.get(key, '')) for key in ('executable', 'config', 'url')}
        ttk.Label(box, text='首次配置一次，之后即可一键启动。\n在 OpenFrp 控制台创建隧道：本地 IP 127.0.0.1，本地端口 17860。\nTCP 隧道使用 http://节点地址:远程端口，下载隧道配置文件。', wraplength=590).pack(anchor='w', pady=(0, 12))
        def browse(key):
            types = [('OpenFrp frpc', '*.exe')] if key == 'executable' else [('隧道配置', '*.ini *.toml *.json *.yaml *.yml')]
            path = filedialog.askopenfilename(parent=window, title='选择客户端' if key == 'executable' else '选择隧道配置文件', filetypes=types)
            if path:
                values[key].set(path)
        for key, title in [('executable', 'OpenFrp frpc 程序'), ('config', '隧道配置文件'), ('url', '公网 HTTP / HTTPS 入口地址')]:
            ttk.Label(box, text=title).pack(anchor='w')
            row = ttk.Frame(box)
            row.pack(fill='x', pady=(3, 10))
            ttk.Entry(row, textvariable=values[key], width=65).pack(side='left', fill='x', expand=True)
            if key != 'url':
                ttk.Button(row, text='选择文件', command=lambda k=key:browse(k)).pack(side='left', padx=6)
        def save():
            try:
                self.openfrp.save({k:v.get() for k,v in values.items()})
            except (ValueError, OSError) as exc:
                messagebox.showerror('配置未保存', str(exc), parent=window)
                return
            self.openfrp_status.set('已配置，点击一键启动')
            window.destroy()
        row = ttk.Frame(box)
        row.pack(fill='x')
        ttk.Button(row, text='打开官方配置指南', command=lambda:webbrowser.open('https://docs.openfrp.net/use/frpc')).pack(side='left')
        ttk.Button(row, text='保存配置', command=save).pack(side='right')
        self.place_popup(window)

    def start_openfrp(self):
        if not self.require():
            return
        if self.openfrp.running:
            self.status.set('OpenFrp 已运行，无需重复启动。')
            return
        if not self.ensure_server():
            return
        try:
            self.openfrp.start()
        except (ValueError, OSError) as exc:
            messagebox.showerror('OpenFrp 启动失败', str(exc), parent=self)
            return
        self.public_url.set(self.openfrp.settings['url'])
        self.save_public_url()
        self.openfrp_status.set('OpenFrp 正在连接节点…')

    def stop_openfrp(self):
        self.openfrp.stop()
        self.openfrp_status.set('OpenFrp 已停止')

    def poll_openfrp(self):
        try:
            while True:
                self.openfrp_status.set(self.openfrp.events.get_nowait())
        except queue.Empty:
            pass
        self.after(500, self.poll_openfrp)

    def publish(self):
        gid = self.selected_group()
        if not gid:
            return
        items = [copy.deepcopy(i) for i in self.project.data['items'] if gid in i['groups']]
        if not items or any(not i['reviewed'] or i['error'] for i in items):
            messagebox.showinfo('请先添加素材', '当前人物没有可发布的素材，或素材读取失败。请先添加照片或视频。', parent=self)
            return
        if not self.ensure_server():
            return
        project = self.project
        def operation(progress):
            for index, item in enumerate(items, 1):
                if fingerprint(Path(item['path'])) != {'size': item['size'], 'mtime': item['mtime']}:
                    raise ValueError('原片发生变化，请重新导入：' + item['name'])
                prepare_web_previews(item, project.cache)
                if item['kind'] == 'video':
                    destination = project.cache / (item['id'] + '.mp4')
                    if not destination.exists():
                        progress(f'生成网页视频预览 {index}/{len(items)}：{item["name"]}')
                        transcode(item['path'], destination)
            with project.lock:
                project.data['groups'][gid]['published'] = True
                project.save()
            return gid
        def done(gid):
            self.current_group = gid
            self.refresh()
            self.status.set('相册已发布。本机预览可测试选片提交；异地访问请配置公网入口。')
        self.run(operation, done)

    def publish_batch(self):
        if not self.require():
            return
        gids = [self.group_ids[n] for n in self.groups.curselection() if self.group_ids[n] in self.project.data['groups']]
        if not gids:
            messagebox.showinfo('选择人物', '请在人物与素材页使用 Ctrl / Shift 选择要发布的人物文件夹。', parent=self)
            return
        if not self.ensure_server():
            return
        from publishing import publish_people
        project = self.project
        def done(results):
            self.refresh(preserve_view=True)
            count = sum(r['status'] == '已发布' for r in results)
            self.status.set(f'批量发布完成：成功 {count} 个，其余 {len(results)-count} 个已跳过或失败。')
            self.show_album_links(results)
        self.run(lambda progress:publish_people(project, gids, progress), done)

    def show_album_links(self, results=None):
        if not self.require():
            return
        data = self.project.snapshot()
        if results is None:
            results = [{'gid':gid, 'status':'已发布' if group['published'] else group.get('last_publish_status', '未发布'),
                        'reason':group.get('last_publish_error', '')} for gid, group in data['groups'].items()]
        window = self.new_popup()
        window.title('人物相册链接清单')
        window.geometry('1000x520')
        box = ttk.Frame(window, padding=16)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='每个人物独立链接。点击选中一行后复制；清单包含接收账号，请按人物发送。').pack(anchor='w')
        base = data.get('public_url') or 'http://127.0.0.1:17860'
        if not data.get('public_url'):
            ttk.Label(box, text='尚未配置公网入口：这些链接只能在本机打开，不能发给异地模特。', foreground='#a45322').pack(anchor='w', pady=6)
        listing = ttk.Treeview(box, columns=('name','contact','link','status','reason'), show='headings', style='Small.Treeview')
        for key,label,width in [('name','人物',120),('contact','QQ / 微信号',160),('link','相册链接',350),('status','状态',95),('reason','原因',180)]:
            listing.heading(key,text=label)
            listing.column(key,width=width)
        listing.pack(fill='both', expand=True, pady=12)
        rows = []
        for r in results:
            group = data['groups'][r['gid']]
            contact = '、'.join(('QQ：' if k=='qq' else '微信：')+v for k,v in group.get('contacts',{}).items() if v)
            link = base+'/a/'+group['token']+'/' if group['published'] else ''
            state = r['status'] if group['published'] or r['status'] != '已发布' else '未发布'
            values = (group['name'], contact, link, state, r['reason'])
            rows.append(values)
            listing.insert('', 'end', iid=r['gid'], values=values)
        def copy_link():
            selected = listing.selection()
            if selected:
                link = listing.item(selected[0], 'values')[2]
                if link:
                    self.clipboard_clear()
                    self.clipboard_append(link)
                    self.status.set('已复制该人物的相册链接。')
        def export():
            path = filedialog.asksaveasfilename(parent=window, title='导出人物链接清单', defaultextension='.csv', initialfile='人物相册链接.csv', filetypes=[('CSV 清单','*.csv')])
            if not path:
                return
            import csv
            try:
                with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
                    writer = csv.writer(handle)
                    writer.writerow(['人物','QQ / 微信号','相册链接','状态','原因'])
                    # Prefix formula-like cells so spreadsheet apps display literal values.
                    writer.writerows([['\''+str(v) if str(v).startswith(('=','+','-','@')) else v for v in row] for row in rows])
                self.status.set('链接清单已导出：'+path)
            except OSError as exc:
                messagebox.showerror('导出失败', str(exc), parent=window)
        buttons = ttk.Frame(box)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='复制选中人物链接', command=copy_link).pack(side='left')
        ttk.Button(buttons, text='导出链接清单 CSV', command=export).pack(side='left', padx=8)
        self.place_popup(window)

    def unpublish_group(self):
        gid = self.selected_group()
        if gid:
            with self.project.lock:
                group = self.project.data['groups'][gid]
                group['published'] = False
                group['token'] = secrets.token_urlsafe(24)
                self.project.save()
            self.refresh()
            self.status.set('当前相册已停止发布，旧链接已失效。')

    def save_public_url(self):
        if not self.require():
            return
        value = self.public_url.get().strip().rstrip('/')
        parsed = urlsplit(value)
        if value and (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            messagebox.showerror('入口格式', '请填写完整 HTTP 或 HTTPS 入口地址（可以带端口），不要附带相册路径、参数或账号密码。', parent=self)
            return
        with self.project.lock:
            self.project.data['public_url'] = value
            self.project.save()
        self.public_url.set(value)
        self.refresh(preserve_view=True)
        self.status.set('入口已保存，尚未验证外网可达性；请用手机移动网络测试。')

    def update_link(self):
        if not self.project:
            return
        group = self.project.data['groups'].get(self.current_group)
        if not group or not group['published']:
            self.link.set('先选择人物并发布相册')
            return
        base = self.project.data.get('public_url') or 'http://127.0.0.1:17860'
        self.link.set(base + '/a/' + group['token'] + '/')

    def active_link(self):
        gid = self.selected_group()
        if not gid:
            return None
        group = self.project.data['groups'][gid]
        if not group['published']:
            messagebox.showinfo('未发布', '请先发布当前人物相册。', parent=self)
            return None
        if not self.ensure_server():
            return None
        return group

    def open_local(self):
        group = self.active_link()
        if group:
            webbrowser.open('http://127.0.0.1:17860/a/' + group['token'] + '/')

    def copy_link(self):
        if self.active_link():
            self.clipboard_clear()
            self.clipboard_append(self.link.get())
            self.status.set('已复制链接。' + ('请勿转发给其他人。' if self.project.data['public_url'] else '当前为本机地址，不能发给朋友；请先配置公网入口。'))

    def show_qr(self):
        if not self.active_link():
            return
        if not self.project.data['public_url']:
            messagebox.showinfo('尚无公网入口', '本机地址不能供手机扫码访问。请先配置公网入口。', parent=self)
            return
        import qrcode
        window = self.new_popup()
        window.title('专属选片二维码（公网入口需在线）')
        image = qrcode.make(self.link.get()).convert('RGB')
        image.thumbnail((400, 400))
        window.qr = ImageTk.PhotoImage(image)
        ttk.Label(window, image=window.qr).pack(padx=20, pady=20)
        self.place_popup(window)

    def network_help(self):
        messagebox.showinfo('公网入口配置', '本机服务地址：http://127.0.0.1:17860\n\n1. 在国内内网穿透客户端中，把入口转发到上述地址。\n2. 将服务商提供的 HTTP 或 HTTPS 根地址填入“公网入口地址”。\n3. 发布人物相册，用手机关闭 Wi-Fi 后测试链接。\n\n此版本不代替服务商注册、实名认证及节点配置。现有 Tailscale 或其他入口也可转发到相同端口。\n填写网址不会自动建立隧道。', parent=self)

    def hide_task_tip(self):
        pending=getattr(self,'_task_tip_after',None)
        if pending:self.after_cancel(pending)
        self._task_tip_after=None
        tip=getattr(self,'_task_tip',None)
        if tip:tip.destroy()
        self._task_tip=None
        self._task_tip_row=None

    def task_hover(self,event):
        ident=self.results.identify_row(event.y)
        if ident==getattr(self,'_task_tip_row',None):return
        self.hide_task_tip()
        names=getattr(self,'_partial_task_names',{}).get(ident)
        if not names:return
        self._task_tip_row=ident
        x,y=event.x_root,event.y_root
        def show():
            self._task_tip_after=None
            tip=tk.Toplevel(self);tip.withdraw();tip.overrideredirect(True)
            self._task_tip=tip
            ttk.Label(tip,text='本任务发送素材：\n'+'\n'.join(names),padding=8,wraplength=520,justify='left',relief='solid').pack()
            tip.update_idletasks()
            tip.geometry(f'+{x+12}+{y+18}');tip.deiconify()
        self._task_tip_after=self.after(450,show)

    def render_results(self, data):
        self._partial_task_names={}
        old = set(self.results.get_children())
        labels = {'queued': '等待发送', 'packing': '历史打包任务', 'ready': '历史压缩包已完成', 'error': '失败', 'waiting': '待发送', 'sending': '正在发送', 'needs_review': '需核对发送结果', 'submitted': '已提交 QQ 发送'}
        for record in reversed(data['submissions']):
            recipient = record.get('recipient', {})
            contact = ('QQ：'+recipient['qq'] if recipient.get('qq') else
                       ('微信：'+recipient['wechat'] if recipient.get('wechat') else '未绑定账号'))
            state_label = labels[record['status']]
            if record['status'] == 'submitted':
                state_label = '已提交 QQ 发送' if recipient.get('qq') else '已提交微信发送'
            elif record['status'] == 'sending':
                state_label = {'packing':'正在打包 ZIP','preparing_originals':'正在准备原片','connecting':'正在定位联系人','sending_archive':'正在发送 ZIP','sending_originals':'正在闪传原片'}.get(record.get('delivery_phase'), state_label)
            scope=record.get('material_scope')
            if scope is None:
                album_ids={item['id'] for item in data['items'] if record['group'] in item.get('groups',[])}
                task_ids=set(record['items'])
                scope='partial' if task_ids and task_ids < album_ids else 'all'
            partial=record.get('source')=='direct' and scope=='partial'
            if partial:self._partial_task_names[record['id']]=[file['name'] for file in record.get('files',[])]
            values = (record['name'] + ((' · 部分素材' if partial else ' · 全部素材') if record.get('source') == 'direct' else ' · 网页选片'), record['created'], len(record['items']), state_label + ('：' + record['error'] if record['error'] else ''), contact + f' · {len(record.get("sent_files", []))}/{len(record["items"])}')
            if record['id'] in old:
                self.results.item(record['id'], values=values)
                old.remove(record['id'])
            else:
                self.results.insert('', 0, iid=record['id'], values=values)
        for ident in old:
            self.results.delete(ident)

    def poll_results(self):
        if self.project:
            self.render_results(self.project.snapshot())
            if not self.busy:
                self.poll_delivery()
        self.after(2000, self.poll_results)

    def save_auto_qq(self):
        if not self.project:
            self.auto_qq.set(False)
            return
        with self.project.lock:
            self.project.data['auto_qq'] = self.auto_qq.get()
            self.project.save()
        self.status.set('自动发送已开启：QQ 闪传原片，微信发送 ZIP。' if self.auto_qq.get() else '自动发送已关闭，任务等待手动启动。')

    def apply_delivery_channel(self, event=None):
        if not self.require() or not self.results.selection():
            return
        choice = {'人物默认':'default','QQ':'qq','微信':'wechat'}[self.delivery_channel.get()]
        with self.project.lock:
            selected = set(self.results.selection())
            prepared = []
            try:
                for record in self.project.data['submissions']:
                    if record['id'] in selected:
                        group = self.project.data['groups'].get(record['group'], {})
                        prepared.append((record, task_recipient(record, group, choice)))
            except ValueError as exc:
                messagebox.showinfo('无法切换发送渠道', str(exc), parent=self)
                return
            for record, recipient in prepared:
                record['recipient'] = recipient
            self.project.save()
        self.render_results(self.project.snapshot())
        self.results.selection_set(list(selected))
        self.status.set('选中任务的发送渠道已更新，请核对接收账号后点击发送。')

    def queue_delivery(self):
        if not self.require() or not self.results.selection():
            return
        with self.project.lock:
            ids = set(self.results.selection())
            records = [s for s in self.project.data['submissions'] if s['id'] in ids and s['status'] != 'submitted']
            prepared = []
            for record in records:
                if record.get('delivery_version') != 1:
                    messagebox.showinfo('历史记录', '选中的记录包含旧任务，请重新提交选片。', parent=self)
                    return
                if record.get('inflight'):
                    messagebox.showinfo('请先核对', '选中任务有发送结果不确定的压缩包，请先核对中断文件。', parent=self)
                    return
                try:
                    choice = {'人物默认':'default','QQ':'qq','微信':'wechat'}[self.delivery_channel.get()]
                    recipient = task_recipient(record, self.project.data['groups'].get(record['group'], {}), choice)
                except ValueError as exc:
                    messagebox.showinfo('接收渠道无法使用', str(exc), parent=self)
                    return
                if not any(recipient.get(c) for c in ('qq', 'wechat')):
                    messagebox.showinfo('需要接收账号', record['name'] + '：请先绑定 QQ 号或微信号。', parent=self)
                    return
                prepared.append((record, dict(recipient)))
            for record, recipient in prepared:
                record.update(status='queued', error='', recipient=recipient)
            self.project.data['delivery_paused'] = False
            self.project.save()
        self.poll_delivery()

    def delete_delivery_tasks(self):
        if not self.require():
            return
        ids = set(self.results.selection())
        if not ids:
            messagebox.showinfo('选择任务', '请使用 Ctrl / Shift 选择要删除的发送任务。', parent=self)
            return
        if self.delivery_active:
            messagebox.showinfo('正在发送', '请等待当前发送结束后再删除任务。', parent=self)
            return
        if not messagebox.askyesno('删除发送任务', f'删除选中的 {len(ids)} 条任务记录？\n照片和视频原片会保留；尚未执行的选中任务将从队列移除。', parent=self):
            return
        try:
            count = self.project.delete_delivery_tasks(ids)
        except (ValueError, OSError) as exc:
            messagebox.showerror('无法删除任务', str(exc), parent=self)
            return
        self.render_results(self.project.snapshot())
        self.status.set(f'已删除 {count} 条发送任务记录，原片保留。')

    def review_delivery(self):
        if not self.require() or not self.results.selection():
            return
        record = next(s for s in self.project.data['submissions'] if s['id'] == self.results.selection()[0])
        ident = record.get('inflight')
        if not ident:
            messagebox.showinfo('无需核对', '没有发送结果不确定的文件。', parent=self)
            return
        batch = record.get('inflight_batch') or [ident]
        name = (f'本批 {len(batch)} 个原片的'+('微信压缩包发送记录' if record.get('delivery_method') == 'wechat_zip' else 'QQ 闪传记录') if record.get('inflight_batch') else
                next(i['name'] for i in record['files'] if i['id'] == ident))
        result = messagebox.askyesnocancel('核对 QQ 文件记录', f'请先在 QQ 核对“{name}”。\n是：QQ 已出现文件记录，跳过此文件。\n否：确认未发送，允许重新发送。\n取消：保持暂停。', parent=self)
        if result is None:
            return
        with self.project.lock:
            if result:
                record['sent_files'].extend(i for i in batch if i not in record['sent_files'])
            record.update(inflight='', inflight_batch=[], status='waiting', error='')
            self.project.save()

    def poll_delivery(self):
        if self.busy or self.delivery_active or not self.project:
            return
        data = self.project.snapshot()
        if data.get('delivery_paused'):
            return
        record = next((s for s in data['submissions'] if s.get('delivery_version') == 1 and
                       (s['status'] == 'queued' or (self.auto_qq.get() and s.get('source') != 'direct' and s['status'] == 'waiting' and any(s.get('recipient',{}).get(c) for c in ('qq','wechat'))))), None)
        if not record:
            return
        project = self.project
        self.delivery_active = True
        def operation(progress):
            try:
                if record.get('recipient', {}).get('qq'):
                    from qq_sender import QQSender
                    sender = QQSender()
                else:
                    from wechat_sender import WeChatSender
                    sender = WeChatSender()
                project.deliver(record['id'], sender)
            except Exception as exc:
                with project.lock:
                    task = next(s for s in project.data['submissions'] if s['id'] == record['id'])
                    task.update(status='error', error=str(exc))
                    project.save()
        def done(_):
            self.delivery_active = False
            with project.lock:
                task = next(s for s in project.data['submissions'] if s['id'] == record['id'])
                if task['status'] in ('error', 'needs_review'):
                    project.data['delivery_paused'] = True
                    project.save()
                    self.status.set('发送队列已暂停：请核对当前 QQ 闪传窗口和失败任务，再点击“发送 / 继续选中任务”。')
            self.render_results(project.snapshot())
            self.render_items(project.snapshot())
        self.run(operation, done)

    def open_project_folder(self):
        if self.project:
            os.startfile(str(self.project.folder))

    def close(self):
        if self.busy:
            messagebox.showinfo('任务仍在运行', '请等待素材预览、视频转码或复制任务完成后关闭。', parent=self)
            return
        self.withdraw()
        self.remember_project()
        self.openfrp.stop()
        if self.server:
            self.server.close()
        if self.project:
            self.project.close()
        self.destroy()

if __name__ == '__main__':
    # Multiple instances keep independent project snapshots and can serve stale links.
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        instance_mutex = kernel.CreateMutexW(None, False, 'Local\\PhotoReturnWorkbenchSingleton')
        if not instance_mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            notice = tk.Tk()
            notice.withdraw()
            messagebox.showinfo('软件已运行', '返图软件已打开，本次启动没有加载新版本。若要使用更新，请先退出原软件，再重新启动。', parent=notice)
            notice.destroy()
            sys.exit(0)
    application = App()
    if len(sys.argv) > 1:
        application.set_project(Path(sys.argv[1]))
    else:
        application.after_idle(application.restore_project)
    application.mainloop()
