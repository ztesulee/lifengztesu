"""Persistent, refreshable task window for the Lightroom delivery backend."""
import tkinter as tk
import os
import time
from pathlib import Path
from tkinter import ttk, messagebox

STATES = {'queued':'等待发送', 'waiting':'重启后暂停', 'sending':'正在发送',
          'pending_review':'已操作发送·待统一核对', 'submitted':'已提交客户端', 'error':'发送失败', 'needs_review':'结果待核对', 'cancelled':'已取消'}
PHASES = {'packing':'打包中', 'preparing_originals':'准备素材', 'connecting':'查找联系人',
          'sending_archive':'提交压缩包', 'sending_originals':'提交原片'}


class TaskWindow(tk.Tk):
    def __init__(self, bridge, token, stop):
        super().__init__()
        self.bridge, self.token, self.stop = bridge, token, stop
        self.title('Lightroom 返图 · 常驻任务窗')
        self.geometry('780x430')
        self.minsize(540, 320)
        self.topmost = tk.BooleanVar(value=False)
        self.pause = tk.BooleanVar(value=False)
        box=ttk.Frame(self,padding=12);box.pack(fill='both',expand=True)
        toolbar=ttk.Frame(box);toolbar.pack(fill='x',pady=(0,8))
        ttk.Label(toolbar,text='Lightroom 返图任务',font=('Microsoft YaHei UI',13,'bold')).pack(side='left')
        ttk.Button(toolbar,text='显示小工具栏',command=self.show_floating_toolbar).pack(side='left',padx=8)
        ttk.Button(toolbar,text='留言模板',command=self.edit_message_template).pack(side='left')
        ttk.Checkbutton(toolbar,text='窗口置顶',variable=self.topmost,command=lambda:self.attributes('-topmost',self.topmost.get())).pack(side='right')
        ttk.Checkbutton(toolbar,text='暂停后续任务',variable=self.pause,command=self.set_pause).pack(side='right',padx=10)
        area=ttk.Frame(box);area.pack(fill='both',expand=True)
        self.table=ttk.Treeview(area,columns=('person','folder','channel','count','state'),show='headings',selectmode='extended')
        for key,title,width in [('person','人物',110),('folder','拍摄文件夹',140),('channel','接收账号',190),('count','提交进度',80),('state','任务状态',150)]:
            self.table.heading(key,text=title);self.table.column(key,width=width,minwidth=65)
        scroll=ttk.Scrollbar(area,orient='vertical',command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.table.pack(fill='both',expand=True)
        self.table.tag_configure('error',foreground='#b42318')
        self.table.tag_configure('needs_review',foreground='#b42318')
        self.table.tag_configure('submitted',foreground='#087f5b')
        self.table.bind('<<TreeviewSelect>>',lambda event:self.show_detail())
        self.table.bind('<Button-3>',self.task_menu)
        self.detail=tk.Text(box,height=3,wrap='word',font=('Microsoft YaHei UI',9),state='disabled')
        self.detail.pack(fill='x',pady=8)
        buttons=ttk.Frame(box);buttons.pack(fill='x')
        ttk.Button(buttons,text='核对 / 继续选中任务',command=self.resume).pack(side='left')
        ttk.Button(buttons,text='取消选中任务',command=self.cancel).pack(side='left',padx=8)
        ttk.Button(buttons,text='终止任务',command=self.terminate).pack(side='left')
        ttk.Button(buttons,text='复制连接口令',command=self.copy_token).pack(side='right')
        self.summary=tk.StringVar()
        ttk.Label(box,textvariable=self.summary,wraplength=730).pack(anchor='w',pady=(8,0))
        self.records={}
        self.floating=None
        self.floating_visible=True
        self.floating_message=tk.StringVar()
        self.floating_status=tk.StringVar()
        self.export_message=tk.StringVar(value='尚未开始导出')
        self.floating_action_buttons=[]
        self.show_floating_toolbar()
        self.after(100,self.withdraw)
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.refresh_tasks()

    def set_pause(self):
        with self.bridge.lock:self.bridge.paused=self.pause.get()

    def edit_message_template(self):
        window=tk.Toplevel(self);window.title('返图后留言模板');window.geometry('650x440')
        box=ttk.Frame(window,padding=12);box.pack(fill='both',expand=True)
        ttk.Label(box,text='账号、人物名称、发送日期和自动发送标识由系统填写。正文可编辑。',wraplength=610).pack(anchor='w')
        editor=tk.Text(box,height=6,wrap='word',font=('Microsoft YaHei UI',11))
        editor.pack(fill='both',expand=True,pady=8)
        editor.insert('1.0',self.bridge.message_body())
        preview=tk.StringVar()
        ttk.Label(box,textvariable=preview,wraplength=610).pack(anchor='w',pady=8)
        def update_preview(event=None):
            body=editor.get('1.0','end-1c').strip()
            preview.set('预览：\nQQ/微信号：示例账号\n示例人物老师您好，\n'+body+'\n日期：'+time.strftime('%Y年%m月%d日 %H点%M分')+'\n（本消息为系统自动发送）')
        editor.bind('<KeyRelease>',update_preview);update_preview()
        row=ttk.Frame(box);row.pack(fill='x')
        def save():
            try:self.bridge.save_message_body(editor.get('1.0','end-1c'))
            except ValueError as exc:messagebox.showerror('无法保存',str(exc),parent=window);return
            window.destroy()
        ttk.Label(box,text='保存后用于尚未开始发送的任务；已开始的任务保持原留言。',wraplength=610).pack(anchor='w',pady=6)
        ttk.Button(row,text='保存模板',command=save).pack(side='right')
        ttk.Button(row,text='取消',command=window.destroy).pack(side='right',padx=8)

    def copy_token(self):
        self.clipboard_clear();self.clipboard_append(self.token)

    def selected(self):
        ids=self.table.selection()
        return self.records.get(ids[0]) if ids else None

    def show_detail(self):
        record=self.selected()
        text=(record['id']+'\n'+record.get('error','')) if record else '选中任务查看详情。已提交客户端不代表对方接收完成。'
        self.detail.configure(state='normal');self.detail.delete('1.0','end');self.detail.insert('1.0',text);self.detail.configure(state='disabled')

    def refresh_tasks(self):
        tasks=self.bridge.tasks(nonblocking=True);self.records={r['id']:r for r in tasks}
        if self.floating and self.floating.winfo_exists():
            if self.floating_visible and self.floating.state()=='withdrawn':self.floating.deiconify()
            for button in self.floating_action_buttons:
                button.configure(state='disabled' if self.bridge.sending or self.bridge.importing else 'normal')
            active=next((r for r in tasks if r['status']=='sending'),None)
            pending=sum(r['status'] in ('queued','waiting','error','needs_review','pending_review') for r in tasks)
            if self.bridge.importing:
                status='正在整理导出素材并创建任务，请稍候'
            elif self.bridge.sending:
                status='正在发送素材'
                if active:
                    status+=f' · {active["name"]} · {active.get("sent",0)}/{active["count"]} · '+PHASES.get(active.get('phase'),'处理中')
            elif pending:
                status=f'未发送素材：{pending} 个任务'+(' · 队列已暂停' if self.bridge.paused else ' · 有待核对任务' if any(r['status'] in ('waiting','error','needs_review') for r in tasks) else ' · 等待发送')
            elif tasks:
                status='暂无待发送素材'
            else:
                status='未发送素材 · 暂无发送任务'
            self.floating_status.set(status)
            progress=dict(self.bridge.export_progress)
            self.export_bar.configure(maximum=max(progress['total'],1),value=progress['done'])
            self.export_message.set(progress['message']+(' · '+self.bridge.prefetch_message if self.bridge.prefetch_message else ''))
            connected=time.monotonic()-self.bridge.plugin_seen<=15
            self.floating_message.set(self.bridge.toolbar_message if connected else '暂未收到 Lightroom 插件响应；若正在处理请稍候，否则检查插件是否启用')
        self.pause.set(self.bridge.paused)
        for ident in self.table.get_children():
            if ident not in self.records:self.table.delete(ident)
        for record in reversed(tasks):
            recipient=record.get('recipient',{})
            account=('QQ：'+recipient['qq']) if recipient.get('qq') else '微信：'+recipient.get('wechat','')
            state=STATES.get(record['status'],record['status'])
            if record['status']=='sending':state+=' · '+PHASES.get(record.get('phase'),'处理中')
            folder=record.get('folder','').split('|')[-1].replace('\\','/').rstrip('/').split('/')[-1]
            values=(record['name'],folder,account,f'{record.get("sent",0)}/{record["count"]}',state)
            if self.table.exists(record['id']):self.table.item(record['id'],values=values,tags=(record['status'],))
            else:self.table.insert('','end',iid=record['id'],values=values,tags=(record['status'],))
        paused=self.bridge.paused
        self.summary.set(('队列已暂停，可取消勾选“暂停后续任务”或继续选中任务。' if paused else '后台运行中；旧任务可多选后点击“核对 / 继续选中任务”批量恢复。')+' 已提交仅表示客户端交接；自动发送时请勿操作鼠标键盘。'+self.bridge.import_notice)
        self.show_detail()
        self.after(800,self.refresh_tasks)

    def resume(self):
        records=[self.records[sid] for sid in self.table.selection() if sid in self.records]
        records=[r for r in records if r['status'] in ('error','waiting','needs_review','cancelled','queued')]
        if not records:return
        cancelled=[r for r in records if r['status']=='cancelled']
        if cancelled and not messagebox.askyesno('恢复已取消任务',f'选中任务中有 {len(cancelled)} 项已取消，是否恢复？已经提交的素材将跳过。',parent=self):
            records=[r for r in records if r['status']!='cancelled']
        values=[]
        for record in records:
            value={'id':record['id']}
            if record['status']=='needs_review' or record.get('inflight'):
                answer=messagebox.askyesnocancel('核对客户端',f"人物：{record['name']}\n请检查 QQ / 微信，本任务的不确定批次是否已经发送？\n\n是：跳过该批次。\n否：重发该批次，可能产生重复附件。\n取消：暂不处理此任务。",parent=self)
                if answer is None:continue
                value['resolution']='skip' if answer else 'retry'
            values.append(value)
        result=self.bridge.resume_many(values)
        if result['errors']:
            messagebox.showerror('部分任务无法继续',f"已恢复 {len(result['resumed'])} 项。\n"+'\n'.join(result['errors']),parent=self)

    def cancel(self):
        record=self.selected()
        if not record:return
        if not messagebox.askyesno('取消任务','取消选中任务？已经提交到客户端的附件不会撤回。',parent=self):return
        try:self.bridge.resume({'id':record['id'],'resolution':'cancel'})
        except ValueError as exc:messagebox.showerror('无法取消',str(exc),parent=self)

    def close(self):
        with self.bridge.lock:
            if self.bridge.sending or self.bridge.importing:
                messagebox.showinfo('正在发送','请等待当前发送结束后退出。可最小化窗口，或暂停后续任务。',parent=self)
                return
            if not messagebox.askyesno('退出返图后台','关闭任务窗会退出返图后台。是否退出？',parent=self):return
            self.bridge.paused=True
            self.stop.set()
        self.destroy()

    def terminate(self):
        record=self.selected()
        if not record:return
        if not messagebox.askyesno('终止任务','停止此任务的后续步骤？已提交的附件不会撤回。当前操作将在下一个检查点停止，队列将暂停。',parent=self):return
        try:self.bridge.terminate(record['id'])
        except ValueError as exc:messagebox.showerror('无法终止',str(exc),parent=self)

    def task_menu(self,event):
        ident=self.table.identify_row(event.y)
        if not ident:return
        if ident not in self.table.selection():self.table.selection_set(ident)
        menu=tk.Menu(self,tearoff=False)
        menu.add_command(label='打开任务图片位置',command=self.open_image_folder)
        menu.add_separator()
        menu.add_command(label='终止任务',command=self.terminate)
        menu.add_command(label='删除选中任务',command=self.delete_selected)
        try:menu.tk_popup(event.x_root,event.y_root)
        finally:menu.grab_release()

    def delete_selected(self):
        ids=list(self.table.selection())
        if not ids:return
        if not messagebox.askyesno('删除任务记录',f'删除 {len(ids)} 个任务记录？\n不会删除照片、视频或 ZIP，也不会撤回客户端附件。',parent=self):return
        try:self.bridge.delete_tasks(ids)
        except ValueError as exc:messagebox.showerror('无法删除',str(exc),parent=self)

    def open_image_folder(self):
        record=self.selected()
        if not record:return
        folders=record.get('image_folders',[])
        if not folders:
            messagebox.showinfo('图片位置','此任务没有记录素材路径。',parent=self);return
        def open_path(path):
            try:
                if not Path(path).is_dir():raise OSError('素材文件夹不存在或已移动：'+path)
                os.startfile(path)
            except OSError as exc:messagebox.showerror('无法打开图片位置',str(exc),parent=self)
        if len(folders)==1:
            open_path(folders[0]);return
        window=tk.Toplevel(self);window.title('选择任务素材文件夹');window.geometry('650x270')
        box=ttk.Frame(window,padding=12);box.pack(fill='both',expand=True)
        ttk.Label(box,text='任务素材位于多个文件夹，请选择：').pack(anchor='w')
        listing=tk.Listbox(box,exportselection=False);listing.pack(fill='both',expand=True,pady=8)
        for path in folders:listing.insert('end',path)
        listing.selection_set(0)
        def choose():
            selected=listing.curselection()
            if selected:open_path(folders[selected[0]])
        ttk.Button(box,text='打开选中文件夹',command=choose).pack(anchor='e')
        listing.bind('<Double-1>',lambda event:choose())

    def show_floating_toolbar(self):
        self.floating_visible=True
        if self.floating and self.floating.winfo_exists():
            self.floating.deiconify();return
        window=tk.Toplevel(self);self.floating=window
        window.title('摄影返图工具栏')
        window.geometry('610x260')
        window.resizable(False,False)
        window.attributes('-topmost',True)
        frame=ttk.Frame(window,padding=8);frame.pack(fill='both',expand=True)
        buttons=ttk.Frame(frame);buttons.pack(fill='x')
        ttk.Style(self).configure('Floating.TButton',font=('Microsoft YaHei UI',10),padding=(6,5))
        actions=[('绑定人物文件夹','bind'),('绑定父文件夹 / 继承','bind_parent'),('文件夹返图状态','folder_status'),
                 ('发送当前文件夹全部素材','folder'),('发送已选中素材','selected'),('连接设置与任务状态','settings')]
        for index,(title,action) in enumerate(actions):
            button=ttk.Button(buttons,text=title,style='Floating.TButton',command=lambda action=action:self.toolbar_action(action))
            button.grid(row=index//3,column=index%3,sticky='ew',padx=3,pady=3)
            self.floating_action_buttons.append(button)
        for column in range(3):buttons.columnconfigure(column,weight=1,uniform='actions')
        extras=ttk.Frame(frame);extras.pack(fill='x',pady=(6,0))
        ttk.Button(extras,text='实时任务窗',command=self.show_tasks).pack(side='left')
        ttk.Button(extras,text='复制连接口令',command=self.copy_token).pack(side='left',padx=6)
        ttk.Label(frame,textvariable=self.floating_status,font=('Microsoft YaHei UI',10,'bold'),wraplength=580).pack(anchor='w',pady=(6,0))
        ttk.Label(frame,textvariable=self.floating_message,wraplength=580).pack(anchor='w',pady=(6,0))
        ttk.Label(frame,textvariable=self.export_message,wraplength=580).pack(anchor='w',pady=(6,0))
        self.export_bar=ttk.Progressbar(frame,mode='determinate',maximum=1)
        self.export_bar.pack(fill='x',pady=(4,0))
        window.protocol('WM_DELETE_WINDOW',self.hide_floating_toolbar)

    def toolbar_action(self,action):
        try:self.bridge.request_toolbar(action)
        except ValueError as exc:messagebox.showinfo('暂时无法操作',str(exc),parent=self.floating)

    def show_tasks(self):
        self.deiconify();self.lift()

    def hide_floating_toolbar(self):
        self.floating_visible=False
        self.show_tasks()
        self.floating.withdraw()
