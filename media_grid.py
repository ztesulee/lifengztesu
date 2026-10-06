"""Scrollable thumbnail grid with the selection API used by the workbench."""
import tkinter as tk
from PIL import Image, ImageTk

class MediaGrid(tk.Canvas):
    def __init__(self, master, **kwargs):
        super().__init__(master, background='#eef1ed', highlightthickness=0, takefocus=True)
        self.rows = {}
        self.selected = set()
        self.active = ''
        self.anchor = ''
        self.card_width = 210
        self.boxes = {}
        self.pending = None
        self.restore_fraction = None
        super().bind('<ButtonPress-1>', self._click)
        super().bind('<Configure>', self._schedule)
        super().bind('<MouseWheel>', self._wheel)
        for key in ('Left','Right','Up','Down'):
            super().bind('<'+key+'>', self._key)

    def bind(self, sequence=None, func=None, add=None):
        return super().bind(sequence, func, '+' if sequence == '<ButtonPress-1>' else add)

    def _wheel(self,event):
        self.yview_scroll(-int(event.delta/120)*3, 'units')
        return 'break'

    def yview(self, *args):
        result = super().yview(*args)
        if args:self._schedule()
        return result

    def yview_scroll(self, number, what):
        super().yview_scroll(number, what)
        self._schedule()

    def yview_moveto(self, fraction):
        super().yview_moveto(fraction)
        self._schedule()

    def restore_scroll(self, fraction):
        # Apply after the new folder's scroll region has been measured.
        self.restore_fraction = fraction
        self._schedule()

    def scroll_position(self):
        return self.restore_fraction if self.restore_fraction is not None else self.yview()[0]

    def _schedule(self,event=None):
        if self.pending:
            self.after_cancel(self.pending)
        self.pending = self.after_idle(self._draw)

    def _draw(self):
        self.pending = None
        super().delete('all')
        width=max(1,self.winfo_width())
        cols=max(1,width//self.card_width)
        cell=max(self.card_width,width//cols)
        height=int(self.card_width*.72)+80
        self.configure(scrollregion=(0,0,width,max(1,((len(self.rows)+cols-1)//cols)*height)),yscrollincrement=24)
        if self.restore_fraction is not None:
            super().yview_moveto(self.restore_fraction)
            self.restore_fraction = None
        top=self.canvasy(0)-height
        bottom=top+self.winfo_height()+2*height
        self.boxes={}
        for index,(ident,row) in enumerate(self.rows.items()):
            x=(index%cols)*cell+6;y=(index//cols)*height+6
            self.boxes[ident]=(x,y,x+cell-12,y+height-12)
            if y+height<top or y>bottom:
                if row.get('path'):row['image']=''
                continue
            if row.get('path') and not row['image']:
                try:
                    with Image.open(row['path']) as source:
                        source.thumbnail((self.card_width-28, int(self.card_width*.72)-20))
                        row['image']=ImageTk.PhotoImage(source.copy(), master=self)
                except (OSError, ValueError):
                    pass
            color='#cce5dc' if ident in self.selected else 'white'
            self.create_rectangle(x,y,x+cell-12,y+height-12,fill=color,outline='#31846d' if ident in self.selected else '#dde3dd',width=2 if ident in self.selected else 1)
            if row['image']:
                self.create_image(x+(cell-12)/2,y+(height-78)/2,image=row['image'])
            else:
                self.create_text(x+(cell-12)/2,y+(height-78)/2,text='ZIP 压缩包' if row['values'][2]=='返图压缩包' else '暂无预览',fill='#88958d')
            name,person,state=row['values']
            self.create_text(x+10,y+height-73,text=name,anchor='nw',width=cell-32,font=('Microsoft YaHei UI',10),fill='#183e35')
            label='该文件已删除' if state=='该文件已删除' else person+' · '+('已移动原片' if '原片已移动' in state else state)
            if len(label)>24:label=label[:23]+'…'
            self.create_text(x+10,y+height-29,text=label,anchor='nw',font=('Microsoft YaHei UI',9),fill='#c53932' if state=='该文件已删除' else '#65786c')
            if row['values'][2] != '返图压缩包':
                right=x+cell-18;badge_bottom=y+height-82
                self.create_rectangle(right-88,badge_bottom-23,right,badge_bottom,fill='white',outline='#dde3dd')
                self.create_text(right-5,badge_bottom-11,anchor='e',text='✓ 已返图' if row.get('returned') else '× 未返图',
                                 font=('Microsoft YaHei UI',9,'bold'),fill='#168044' if row.get('returned') else '#c53932')

    def identify_row(self,y,x=None):
        if x is None:x=self.winfo_pointerx()-self.winfo_rootx()
        cx,cy=self.canvasx(x),self.canvasy(y)
        return next((i for i,(a,b,c,d) in self.boxes.items() if a<=cx<=c and b<=cy<=d),'')

    def _click(self,event):
        self.focus_set()
        ident=self.identify_row(event.y,event.x)
        if not ident:
            if not event.state & 5:self.selection_set([])
            return
        ids=list(self.rows)
        if event.state & 1 and self.anchor in self.rows:
            a,b=sorted((ids.index(self.anchor),ids.index(ident)))
            values=set(ids[a:b+1])
            self.selection_set(self.selected|values if event.state & 4 else values)
        elif event.state & 4:
            self.selection_set(self.selected^{ident});self.anchor=ident
        else:
            if ident not in self.selected:self.selection_set([ident])
            self.anchor=ident
        self.active=ident

    def _key(self,event):
        ids=list(self.rows)
        if not ids:return 'break'
        index=ids.index(self.active) if self.active in ids else 0
        cols=max(1,self.winfo_width()//self.card_width)
        offset={'Left':-1,'Right':1,'Up':-cols,'Down':cols}[event.keysym]
        ident=ids[min(len(ids)-1,max(0,index+offset))]
        if event.state & 1 and self.anchor in ids:
            a,b=sorted((ids.index(self.anchor),ids.index(ident)));self.selection_set(ids[a:b+1])
        elif not event.state & 4:self.selection_set([ident]);self.anchor=ident
        self.active=ident
        box=self.boxes.get(ident)
        if box:
            top=self.canvasy(0);bottom=top+self.winfo_height()
            if box[1]<top or box[3]>bottom:
                total=float(self.cget('scrollregion').split()[3]);self.yview_moveto(box[1]/max(1,total))
        return 'break'

    def get_children(self):return tuple(self.rows)
    def selection(self):return tuple(i for i in self.rows if i in self.selected)
    def selection_set(self,values):
        if isinstance(values,str):values=[values]
        self.selected=set(values)&self.rows.keys();self._schedule();self.event_generate('<<TreeviewSelect>>')
    def focus(self,ident=None):
        if ident is not None:self.active=ident
        return self.active
    def delete(self,*ids):
        for ident in ids:self.rows.pop(ident,None);self.selected.discard(ident)
        self._schedule()
    def insert(self,parent,index,iid,image='',values=(),path=None,returned=False):
        self.rows[iid]={'image':image,'values':values,'path':path,'returned':returned};self._schedule();return iid
