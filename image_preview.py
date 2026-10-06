"""Mouse-centered image zoom with bounded viewport rendering."""
import tkinter as tk
import sys
from PIL import Image, ImageTk


def preview_photo_ids(visible_ids, items):
    photos={item['id'] for item in items if item.get('kind')=='photo'}
    return [ident for ident in visible_ids if ident in photos]


class PreviewFullscreen:
    def __init__(self, window):
        self.window=window
        self.enabled=False
        self.saved=None

    def set(self, enabled):
        if enabled==self.enabled:return
        window=self.window
        if sys.platform!='win32':
            window.attributes('-fullscreen',enabled)
            self.enabled=enabled
            return
        import win32api, win32gui, win32con
        window.update_idletasks()
        hwnd=win32gui.GetAncestor(window.winfo_id(),2) or window.winfo_id()
        if enabled:
            self.saved=(win32gui.GetWindowRect(hwnd),window.geometry(),window.overrideredirect())
            monitor=win32api.MonitorFromWindow(hwnd,win32con.MONITOR_DEFAULTTONEAREST)
            bounds=win32api.GetMonitorInfo(monitor)['Monitor']
            window.overrideredirect(True)
        else:
            bounds,geometry,override=self.saved
            window.overrideredirect(override)
            window.geometry(geometry.split('+')[0].split('-')[0])
        left,top,right,bottom=bounds
        if enabled:window.geometry(f'{right-left}x{bottom-top}')
        window.update_idletasks()
        hwnd=win32gui.GetAncestor(window.winfo_id(),2) or window.winfo_id()
        # Signed screen coordinates also cover monitors left/above the primary.
        win32gui.SetWindowPos(hwnd,0,left,top,right-left,bottom-top,
                              win32con.SWP_NOZORDER|win32con.SWP_NOOWNERZORDER|win32con.SWP_FRAMECHANGED)
        self.enabled=enabled


class ZoomPreview(tk.Canvas):
    def __init__(self, master, image):
        super().__init__(master,background='#292929',highlightthickness=0,width=900,height=650)
        self.source=image.convert('RGB')
        self.scale=1
        self.x=self.y=0
        self.fitted=True
        self.photo=None
        self.drag=None
        self.bind('<Configure>',self.resize)
        self.bind('<MouseWheel>',self.zoom)
        self.bind('<ButtonPress-1>',self.start_drag)
        self.bind('<B1-Motion>',self.pan)
        self.bind('<Destroy>',self.dispose)

    def dispose(self,event):
        if event.widget is self:self.source.close()

    def set_image(self,image):
        replacement=image.convert('RGB')
        self.source.close()
        self.source=replacement
        self.drag=None
        self.fit()

    def fit(self):
        self.fitted=True
        width,height=max(1,self.winfo_width()),max(1,self.winfo_height())
        self.scale=min(width/self.source.width,height/self.source.height,1)
        self.x=(width-self.source.width*self.scale)/2
        self.y=(height-self.source.height*self.scale)/2
        self.draw()

    def resize(self,event):
        if self.fitted:self.fit()
        else:self.draw()

    def zoom(self,event):
        if not event.delta:return 'break'
        factor=1.2 if event.delta>0 else 1/1.2
        scale=min(8,max(.05,self.scale*factor))
        ratio=scale/self.scale
        self.x=event.x-(event.x-self.x)*ratio
        self.y=event.y-(event.y-self.y)*ratio
        self.scale=scale;self.fitted=False
        self.draw()
        return 'break'

    def start_drag(self,event):
        self.drag=(event.x,event.y,self.x,self.y)

    def pan(self,event):
        if self.drag:
            x,y,left,top=self.drag
            self.x=left+event.x-x;self.y=top+event.y-y
            self.fitted=False;self.draw()

    def draw(self):
        width,height=max(1,self.winfo_width()),max(1,self.winfo_height())
        # Render the visible viewport rather than allocating a huge zoomed image.
        frame=self.source.transform((width,height),Image.Transform.AFFINE,
            (1/self.scale,0,-self.x/self.scale,0,1/self.scale,-self.y/self.scale),
            resample=Image.Resampling.BICUBIC,fillcolor=(41,41,41))
        self.photo=ImageTk.PhotoImage(frame,master=self)
        self.delete('all');self.create_image(0,0,anchor='nw',image=self.photo)
