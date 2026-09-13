import argparse
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from uuid import uuid4

from PIL import Image, ImageTk


APP_NAME = "文件夹管家"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "FolderPilot"
DATA_FILE = DATA_DIR / "folders.json"
CATEGORY_FILE = DATA_DIR / "categories.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
BACKUP_DIR = DATA_DIR / "backups"
SCRIPT_EXTENSIONS = {".bat", ".cmd", ".ps1", ".py", ".pyw", ".vbs", ".js", ".wsf"}


def resource_path(relative_path):
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path / relative_path


def infer_kind(path):
    if os.path.isdir(path):
        return "folder"
    if Path(path).suffix.lower() in SCRIPT_EXTENSIONS:
        return "script"
    return "file"


def kind_label(kind):
    return {"folder": "文件夹", "file": "文件", "script": "启动脚本"}.get(kind, "文件")


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HANDLE),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def extract_shell_icon(path, size=24):
    """Return a transparent PIL image for the icon Windows assigns to path."""
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    shell32.SHGetFileInfoW.restype = ctypes.c_void_p
    shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(SHFILEINFOW), wintypes.UINT, wintypes.UINT]
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    user32.DrawIconEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.DestroyIcon.argtypes = [ctypes.c_void_p]
    shell_info = SHFILEINFOW()
    flags = 0x000000100 | 0x000000001  # SHGFI_ICON | SHGFI_SMALLICON
    result = shell32.SHGetFileInfoW(
        str(path), 0, ctypes.byref(shell_info), ctypes.sizeof(shell_info), flags
    )
    if not result or not shell_info.hIcon:
        return None

    screen_dc = user32.GetDC(None)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap_info = BITMAPINFO()
    bitmap_info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bitmap_info.bmiHeader.biWidth = size
    bitmap_info.bmiHeader.biHeight = -size
    bitmap_info.bmiHeader.biPlanes = 1
    bitmap_info.bmiHeader.biBitCount = 32
    bitmap_info.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    bitmap = gdi32.CreateDIBSection(
        memory_dc, ctypes.byref(bitmap_info), 0, ctypes.byref(bits), None, 0
    )
    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    try:
        ctypes.memset(bits, 0, size * size * 4)
        user32.DrawIconEx(memory_dc, 0, 0, shell_info.hIcon, size, size, 0, None, 0x0003)
        raw = ctypes.string_at(bits, size * size * 4)
        image = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1).copy()
        if not image.getchannel("A").getbbox():
            pixels = image.load()
            for y in range(size):
                for x in range(size):
                    red, green, blue, _alpha = pixels[x, y]
                    pixels[x, y] = (red, green, blue, 0 if (red, green, blue) == (0, 0, 0) else 255)
        return image
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)
        user32.DestroyIcon(shell_info.hIcon)


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", ctypes.c_void_p),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", ctypes.c_void_p),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class WindowsIntegration:
    WM_HOTKEY = 0x0312
    WM_DROPFILES = 0x0233
    WM_TRAY = 0x8000 + 20

    def __init__(self, app):
        self.app = app
        self.hwnd = app.winfo_id()
        self.user32 = ctypes.windll.user32
        self.shell32 = ctypes.windll.shell32
        self.gdi32 = ctypes.windll.gdi32
        self._configure_api()
        self.callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        self.callback = self.callback_type(self._window_proc)
        setter = self.user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else self.user32.SetWindowLongW
        setter.restype = ctypes.c_void_p
        setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.original_proc = setter(self.hwnd, -4, self.callback)
        self.shell32.DragAcceptFiles(self.hwnd, True)
        self.hotkey_registered = bool(self.user32.RegisterHotKey(self.hwnd, 1, 0x0002 | 0x0001 | 0x4000, 0x20))
        self.tray_data = None
        self.tray_icon = None
        self.add_tray_icon()

    def _configure_api(self):
        self.user32.CallWindowProcW.restype = ctypes.c_ssize_t
        self.user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        self.user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        self.shell32.DragQueryFileW.restype = wintypes.UINT
        self.shell32.DragFinish.argtypes = [ctypes.c_void_p]
        self.user32.LoadImageW.restype = ctypes.c_void_p
        self.user32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        self.user32.CreatePopupMenu.restype = ctypes.c_void_p
        self.user32.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        self.user32.TrackPopupMenu.restype = wintypes.UINT
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        self.user32.DestroyMenu.argtypes = [ctypes.c_void_p]

    def add_tray_icon(self):
        icon_path = str(resource_path("assets/FolderPilot.ico"))
        self.tray_icon = self.user32.LoadImageW(None, icon_path, 1, 0, 0, 0x0010 | 0x0040)
        if not self.tray_icon:
            return
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = 0x0001 | 0x0002 | 0x0004
        data.uCallbackMessage = self.WM_TRAY
        data.hIcon = self.tray_icon
        data.szTip = APP_NAME
        self.shell32.Shell_NotifyIconW(0x00000000, ctypes.byref(data))
        self.tray_data = data

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == self.WM_HOTKEY:
            self.app.after(0, self.app.show_and_search)
            return 0
        if message == self.WM_DROPFILES:
            count = self.shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
            paths = []
            for index in range(count):
                length = self.shell32.DragQueryFileW(wparam, index, None, 0)
                buffer = ctypes.create_unicode_buffer(length + 1)
                self.shell32.DragQueryFileW(wparam, index, buffer, length + 1)
                paths.append(buffer.value)
            self.shell32.DragFinish(wparam)
            self.app.after(0, lambda: self.app.add_dropped_paths(paths))
            return 0
        if message == self.WM_TRAY:
            if lparam in (0x0202, 0x0203):
                self.app.after(0, self.app.show_window)
            elif lparam == 0x0205:
                self.show_tray_menu()
            return 0
        return self.user32.CallWindowProcW(self.original_proc, hwnd, message, wparam, lparam)

    def show_tray_menu(self):
        menu = self.user32.CreatePopupMenu()
        self.user32.AppendMenuW(menu, 0, 1, "打开文件夹管家")
        recent = sorted((item for item in self.app.items if item.get("last_opened_at")), key=lambda item: item.get("last_opened_at", ""), reverse=True)[:5]
        if recent:
            self.user32.AppendMenuW(menu, 0x0800, 0, None)
            for index, item in enumerate(recent):
                self.user32.AppendMenuW(menu, 0, 1000 + index, item.get("name", "未命名"))
        self.user32.AppendMenuW(menu, 0x0800, 0, None)
        self.user32.AppendMenuW(menu, 0, 2, "退出")
        point = POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        self.user32.SetForegroundWindow(self.hwnd)
        command = self.user32.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y, 0, self.hwnd, None)
        self.user32.DestroyMenu(menu)
        if command == 1:
            self.app.after(0, self.app.show_window)
        elif command == 2:
            self.app.after(0, self.app.exit_application)
        elif command >= 1000 and command - 1000 < len(recent):
            self.app.after(0, lambda item=recent[command - 1000]: self.app.open_item(item))

    def cleanup(self):
        self.user32.UnregisterHotKey(self.hwnd, 1)
        self.shell32.DragAcceptFiles(self.hwnd, False)
        if self.tray_data is not None:
            self.shell32.Shell_NotifyIconW(0x00000002, ctypes.byref(self.tray_data))
        if self.tray_icon:
            self.user32.DestroyIcon(self.tray_icon)


def load_items():
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        for item in data:
            item["kind"] = item.get("kind") or infer_kind(item.get("path", ""))
        return data
    except (OSError, json.JSONDecodeError) as exc:
        messagebox.showwarning(APP_NAME, f"数据文件读取失败，将使用空列表。\n\n{exc}")
        return []


def save_items(items):
    create_backup()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = DATA_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_file.replace(DATA_FILE)


def create_backup():
    if not DATA_FILE.exists():
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        items = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        categories = []
        if CATEGORY_FILE.exists():
            categories = json.loads(CATEGORY_FILE.read_text(encoding="utf-8"))
        backup = BACKUP_DIR / f"backup-{stamp}.json"
        backup.write_text(json.dumps({"version": 1, "items": items, "categories": categories}, ensure_ascii=False, indent=2), encoding="utf-8")
        for old_file in sorted(BACKUP_DIR.glob("backup-*.json"), reverse=True)[10:]:
            old_file.unlink()
    except (OSError, json.JSONDecodeError):
        pass


def load_settings():
    try:
        value = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(settings):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_categories(items):
    saved = []
    if CATEGORY_FILE.exists():
        try:
            value = json.loads(CATEGORY_FILE.read_text(encoding="utf-8"))
            if isinstance(value, list):
                saved = [str(name).strip() for name in value if str(name).strip()]
        except (OSError, json.JSONDecodeError):
            pass
    used = [item.get("category", "未分类") for item in items]
    return list(dict.fromkeys(["工作", "学习", "项目", "素材", "临时", "未分类"] + saved + used))


def save_categories(categories):
    create_backup()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CATEGORY_FILE.write_text(json.dumps(categories, ensure_ascii=False, indent=2), encoding="utf-8")


class CategoryDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.app = parent
        self.title("管理分类")
        self.geometry("470x440")
        self.minsize(430, 380)
        self.transient(parent)
        self.grab_set()
        self.configure(bg="#f4f6fa")

        body = ttk.Frame(self, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="自定义分类", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(body, text="新增、重命名或删除分类。删除分类不会删除真实项目。", foreground="#697386").pack(anchor="w", pady=(5, 14))

        add_row = ttk.Frame(body)
        add_row.pack(fill="x", pady=(0, 12))
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(add_row, textvariable=self.name_var)
        self.name_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(add_row, text="＋ 新增", style="Primary.TButton", command=self.add_category).pack(side="left", padx=(8, 0))

        list_frame = ttk.Frame(body, style="Card.TFrame")
        list_frame.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(list_frame, relief="flat", bd=0, highlightthickness=1, highlightbackground="#dfe4ec", selectbackground="#dbe7ff", selectforeground="#1e3157", font=("Microsoft YaHei UI", 11), activestyle="none")
        self.listbox.pack(fill="both", expand=True, padx=1, pady=1)

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="删除", command=self.delete_category).pack(side="right", padx=(0, 8))
        ttk.Button(actions, text="重命名", command=self.rename_category).pack(side="right", padx=(0, 8))

        self.name_entry.bind("<Return>", lambda _event: self.add_category())
        self.listbox.bind("<Double-1>", lambda _event: self.rename_category())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.refresh()
        self.after(80, self.name_entry.focus_set)

    def refresh(self, select_name=None):
        self.listbox.delete(0, "end")
        for category in self.app.custom_categories:
            self.listbox.insert("end", category)
        if select_name in self.app.custom_categories:
            index = self.app.custom_categories.index(select_name)
            self.listbox.selection_set(index)
            self.listbox.see(index)

    def selected(self):
        selection = self.listbox.curselection()
        return self.listbox.get(selection[0]) if selection else None

    def add_category(self):
        name = self.name_var.get().strip()
        if not name:
            return
        if name in ("全部", "★ 已置顶") or any(name.casefold() == value.casefold() for value in self.app.custom_categories):
            messagebox.showinfo(APP_NAME, "这个分类已经存在。", parent=self)
            return
        self.app.custom_categories.append(name)
        save_categories(self.app.custom_categories)
        self.name_var.set("")
        self.refresh(name)
        self.app.refresh_categories()

    def rename_category(self):
        old_name = self.selected()
        if not old_name:
            messagebox.showinfo(APP_NAME, "请先选择一个分类。", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("重命名分类")
        dialog.geometry("390x165")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"将「{old_name}」重命名为：").pack(anchor="w")
        value = tk.StringVar(value=old_name)
        entry = ttk.Entry(frame, textvariable=value)
        entry.pack(fill="x", pady=(8, 14))
        result = {"ok": False}

        def confirm():
            new_name = value.get().strip()
            if not new_name:
                return
            if new_name != old_name and any(new_name.casefold() == name.casefold() for name in self.app.custom_categories):
                messagebox.showinfo(APP_NAME, "这个分类已经存在。", parent=dialog)
                return
            result["ok"] = True
            result["name"] = new_name
            dialog.destroy()

        button_row = ttk.Frame(frame)
        button_row.pack(anchor="e")
        ttk.Button(button_row, text="取消", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="确定", style="Primary.TButton", command=confirm).pack(side="left")
        entry.bind("<Return>", lambda _event: confirm())
        entry.focus_set()
        entry.select_range(0, "end")
        self.wait_window(dialog)
        if not result["ok"]:
            return
        new_name = result["name"]
        index = self.app.custom_categories.index(old_name)
        self.app.custom_categories[index] = new_name
        for item in self.app.items:
            if item.get("category") == old_name:
                item["category"] = new_name
        if self.app.category == old_name:
            self.app.category = new_name
        save_categories(self.app.custom_categories)
        save_items(self.app.items)
        self.refresh(new_name)
        self.app.refresh_categories()
        self.app.refresh_list()

    def delete_category(self):
        name = self.selected()
        if not name:
            messagebox.showinfo(APP_NAME, "请先选择一个分类。", parent=self)
            return
        count = sum(item.get("category") == name for item in self.app.items)
        detail = f"\n\n其中的 {count} 个项目将移动到“未分类”。" if count else ""
        if not messagebox.askyesno(APP_NAME, f"确定删除分类「{name}」吗？{detail}", parent=self):
            return
        self.app.custom_categories.remove(name)
        if "未分类" not in self.app.custom_categories:
            self.app.custom_categories.append("未分类")
        for item in self.app.items:
            if item.get("category") == name:
                item["category"] = "未分类"
        if self.app.category == name:
            self.app.category = "全部"
        save_categories(self.app.custom_categories)
        save_items(self.app.items)
        self.refresh()
        self.app.refresh_categories()
        self.app.refresh_list()


class FolderDialog(tk.Toplevel):
    def __init__(self, parent, categories, item=None):
        super().__init__(parent)
        self.result = None
        self.item = item
        self.title("编辑常用项目" if item else "添加常用项目")
        self.geometry("620x535")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.configure(bg="#f4f6fa")

        body = ttk.Frame(self, padding=24)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        self.name_var = tk.StringVar(value=item.get("name", "") if item else "")
        self.path_var = tk.StringVar(value=item.get("path", "") if item else "")
        self.category_var = tk.StringVar(value=item.get("category", "未分类") if item else "未分类")
        self.note_var = tk.StringVar(value=item.get("note", "") if item else "")
        self.keywords_var = tk.StringVar(value=", ".join(item.get("keywords", [])) if item else "")
        self.kind_var = tk.StringVar(value=kind_label(item.get("kind", "folder")) if item else "文件夹")

        ttk.Label(body, text="显示名称", style="Form.TLabel").grid(row=0, column=0, sticky="w")
        self.name_entry = ttk.Entry(body, textvariable=self.name_var)
        self.name_entry.grid(row=1, column=0, sticky="ew", pady=(5, 14))

        ttk.Label(body, text="项目类型", style="Form.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Combobox(body, textvariable=self.kind_var, values=("文件夹", "文件", "启动脚本"), state="readonly").grid(row=3, column=0, sticky="ew", pady=(5, 14))

        ttk.Label(body, text="路径", style="Form.TLabel").grid(row=4, column=0, sticky="w")
        path_row = ttk.Frame(body)
        path_row.grid(row=5, column=0, sticky="ew", pady=(5, 14))
        path_row.columnconfigure(0, weight=1)
        ttk.Entry(path_row, textvariable=self.path_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(path_row, text="选文件夹", command=self.browse_folder).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(path_row, text="选文件", command=self.browse_file).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(body, text="分类", style="Form.TLabel").grid(row=6, column=0, sticky="w")
        values = list(dict.fromkeys(["工作", "学习", "项目", "素材", "临时", "未分类"] + categories))
        ttk.Combobox(body, textvariable=self.category_var, values=values).grid(
            row=7, column=0, sticky="ew", pady=(5, 14)
        )

        ttk.Label(body, text="关键词（用逗号分隔）", style="Form.TLabel").grid(row=8, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.keywords_var).grid(row=9, column=0, sticky="ew", pady=(5, 14))

        ttk.Label(body, text="备注（可选）", style="Form.TLabel").grid(row=10, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.note_var).grid(row=11, column=0, sticky="ew", pady=(5, 18))

        actions = ttk.Frame(body)
        actions.grid(row=12, column=0, sticky="e")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="保存", style="Primary.TButton", command=self.submit).pack(side="left")

        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self.submit())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.after(80, self.name_entry.focus_set)

    def browse_folder(self):
        initial = self.path_var.get().strip()
        folder = filedialog.askdirectory(parent=self, initialdir=initial if os.path.isdir(initial) else None)
        if folder:
            self.path_var.set(os.path.normpath(folder))
            self.kind_var.set("文件夹")
            if not self.name_var.get().strip():
                self.name_var.set(Path(folder).name or folder)

    def browse_file(self):
        initial = self.path_var.get().strip()
        file_path = filedialog.askopenfilename(
            parent=self,
            initialdir=str(Path(initial).parent) if os.path.isfile(initial) else None,
            filetypes=(("所有支持的项目", "*.*"), ("启动脚本", "*.bat *.cmd *.ps1 *.py *.pyw *.vbs *.js *.wsf"), ("所有文件", "*.*")),
        )
        if file_path:
            normalized = os.path.normpath(file_path)
            self.path_var.set(normalized)
            self.kind_var.set(kind_label(infer_kind(normalized)))
            if not self.name_var.get().strip():
                self.name_var.set(Path(normalized).name)

    def submit(self):
        path = os.path.normpath(self.path_var.get().strip())
        if not path or not os.path.exists(path):
            messagebox.showwarning(APP_NAME, "请选择一个存在的文件夹、文件或脚本。", parent=self)
            return
        selected_kind = {"文件夹": "folder", "文件": "file", "启动脚本": "script"}[self.kind_var.get()]
        actual_kind = infer_kind(path)
        if selected_kind == "folder" and actual_kind != "folder":
            messagebox.showwarning(APP_NAME, "所选路径不是文件夹，请修改项目类型。", parent=self)
            return
        if selected_kind != "folder" and actual_kind == "folder":
            messagebox.showwarning(APP_NAME, "所选路径是文件夹，请修改项目类型。", parent=self)
            return
        self.result = {
            "name": self.name_var.get().strip() or Path(path).name or path,
            "path": path,
            "category": self.category_var.get().strip() or "未分类",
            "note": self.note_var.get().strip(),
            "keywords": [word.strip() for word in self.keywords_var.get().replace("，", ",").split(",") if word.strip()],
            "kind": selected_kind,
        }
        self.destroy()


class ScriptConfirmDialog(tk.Toplevel):
    def __init__(self, parent, item):
        super().__init__(parent)
        self.result = False
        self.trust = tk.BooleanVar(value=False)
        self.title("确认运行脚本")
        self.geometry("520x245")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        body = ttk.Frame(self, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="即将运行启动脚本", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(body, text=item.get("name", ""), foreground="#315db3").pack(anchor="w", pady=(10, 2))
        ttk.Label(body, text=item.get("path", ""), foreground="#697386", wraplength=460).pack(anchor="w")
        ttk.Checkbutton(body, text="信任此项目，以后不再询问", variable=self.trust).pack(anchor="w", pady=(15, 12))
        row = ttk.Frame(body)
        row.pack(anchor="e")
        ttk.Button(row, text="取消", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="运行", style="Primary.TButton", command=self.confirm).pack(side="left")
        self.bind("<Escape>", lambda _event: self.destroy())

    def confirm(self):
        self.result = True
        self.destroy()


class FolderPilot(tk.Tk):
    def __init__(self):
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FolderPilot.App.1")
        except (AttributeError, OSError):
            pass
        super().__init__()
        self.settings = load_settings()
        self.title(APP_NAME)
        icon_path = resource_path("assets/FolderPilot.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.geometry(self.settings.get("geometry", "1080x700"))
        self.minsize(860, 540)
        self.configure(bg="#f4f6fa")
        self.items = load_items()
        self.custom_categories = load_categories(self.items)
        self.item_icons = {}
        self.category_editor = None
        self.category = self.settings.get("category", "全部")
        self.category_buttons = []
        self.search_var = tk.StringVar()
        self.sort_var = tk.StringVar(value=self.settings.get("sort", "置顶优先"))
        self.status_var = tk.StringVar()
        self._configure_style()
        self._build_ui()
        self.refresh_categories()
        self.refresh_list()
        self.update_idletasks()
        self.windows = WindowsIntegration(self)
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

    def _configure_style(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TFrame", background="#f4f6fa")
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background="#f4f6fa", foreground="#273043", font=("Microsoft YaHei UI", 10))
        style.configure("Form.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TEntry", padding=9)
        style.configure("TCombobox", padding=7)
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(13, 8))
        style.configure("Primary.TButton", foreground="#17418f", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=38, font=("Microsoft YaHei UI", 10), background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=(7, 9))
        style.map("Treeview", background=[("selected", "#dbe7ff")], foreground=[("selected", "#1e3157")])

    def _build_ui(self):
        header = tk.Frame(self, bg="#273043", height=92)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Button(header, text="？ 帮助", command=self.show_help, bg="#273043", fg="#dbe4f5", activebackground="#34415a", activeforeground="white", relief="flat", bd=0, cursor="hand2", font=("Microsoft YaHei UI", 10), padx=14, pady=8).pack(side="left", padx=(14, 0))
        titles = tk.Frame(header, bg="#273043")
        titles.pack(side="left", padx=16, pady=17)
        tk.Label(titles, text=APP_NAME, bg="#273043", fg="white", font=("Microsoft YaHei UI", 21, "bold")).pack(anchor="w")
        tk.Label(titles, text="集中管理常用文件夹、文件和启动脚本", bg="#273043", fg="#bdc7d9", font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(3, 0))
        tk.Button(header, text="＋ 添加常用项目", command=self.add_item, bg="#4c7cf3", fg="white", activebackground="#3f6ddd", activeforeground="white", relief="flat", bd=0, cursor="hand2", font=("Microsoft YaHei UI", 10, "bold"), padx=19, pady=10).pack(side="right", padx=(8, 26))
        tk.Button(header, text="备份 / 恢复", command=self.show_data_menu, bg="#34415a", fg="#e3e9f4", activebackground="#42506b", activeforeground="white", relief="flat", bd=0, cursor="hand2", font=("Microsoft YaHei UI", 10), padx=14, pady=9).pack(side="right")

        main = tk.Frame(self, bg="#f4f6fa")
        main.pack(fill="both", expand=True, padx=20, pady=18)

        sidebar = tk.Frame(main, bg="white", width=190, highlightbackground="#e2e6ee", highlightthickness=1)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="分类", bg="white", fg="#697386", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", padx=17, pady=(17, 9))
        self.categories_frame = tk.Frame(sidebar, bg="white")
        self.categories_frame.pack(fill="both", expand=True, padx=8)
        tk.Button(sidebar, text="⚙ 管理分类", command=self.manage_categories, anchor="w", relief="flat", bd=0, cursor="hand2", bg="white", fg="#4c67a1", activebackground="#edf2fc", font=("Microsoft YaHei UI", 10), padx=17, pady=11).pack(fill="x", side="bottom", pady=(4, 8))

        content = tk.Frame(main, bg="#f4f6fa")
        content.pack(side="left", fill="both", expand=True, padx=(16, 0))

        toolbar = ttk.Frame(content)
        toolbar.pack(fill="x", pady=(0, 12))
        search_wrap = tk.Frame(toolbar, bg="white", highlightbackground="#d7dce5", highlightthickness=1)
        search_wrap.pack(side="left", fill="x", expand=True)
        tk.Label(search_wrap, text="⌕", bg="white", fg="#7c8799", font=("Segoe UI Symbol", 15)).pack(side="left", padx=(11, 2))
        self.search_entry = tk.Entry(search_wrap, textvariable=self.search_var, relief="flat", bd=0, bg="white", fg="#273043", font=("Microsoft YaHei UI", 10))
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=10, padx=(0, 8))
        self.search_entry.insert(0, "")
        ttk.Combobox(toolbar, textvariable=self.sort_var, values=("置顶优先", "最近打开", "使用次数", "名称排序"), state="readonly", width=12).pack(side="right", padx=(10, 0))

        table_card = tk.Frame(content, bg="white", highlightbackground="#e2e6ee", highlightthickness=1)
        table_card.pack(fill="both", expand=True)
        columns = ("star", "kind", "category", "path", "status", "count")
        self.tree = ttk.Treeview(table_card, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="名称")
        self.tree.column("#0", width=190, minwidth=120, stretch=False, anchor="w")
        headings = (("star", "", 40), ("kind", "类型", 85), ("category", "分类", 90), ("path", "路径", 350), ("status", "状态", 70), ("count", "次数", 55))
        for key, title, width in headings:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=40, stretch=key == "path", anchor="center" if key in ("star", "kind", "status", "count") else "w")
        for key, width in self.settings.get("widths", {}).items():
            if key in ("#0", "star", "kind", "category", "path", "status", "count"):
                try:
                    self.tree.column(key, width=int(width))
                except (ValueError, tk.TclError):
                    pass
        scrollbar = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(7, 0), pady=7)
        scrollbar.pack(side="right", fill="y", pady=7, padx=(0, 7))
        self.empty_label = tk.Label(table_card, text="📁\n\n还没有常用项目\n\n点击右上角“添加常用项目”开始整理", bg="white", fg="#7c8799", justify="center", font=("Microsoft YaHei UI", 11))

        actions = ttk.Frame(content)
        actions.pack(fill="x", pady=(12, 0))
        self.open_button = ttk.Button(actions, text="打开", style="Primary.TButton", command=self.open_selected)
        self.open_button.pack(side="right")
        self.delete_button = ttk.Button(actions, text="删除", command=self.delete_item)
        self.delete_button.pack(side="right", padx=(0, 7))
        self.edit_button = ttk.Button(actions, text="编辑", command=self.edit_item)
        self.edit_button.pack(side="right", padx=(0, 7))
        self.favorite_button = ttk.Button(actions, text="☆ 置顶", command=self.toggle_favorite)
        self.favorite_button.pack(side="right", padx=(0, 7))

        tk.Label(self, textvariable=self.status_var, bg="#e9edf4", fg="#697386", anchor="w", padx=20, pady=7, font=("Microsoft YaHei UI", 9)).pack(fill="x")

        self.search_var.trace_add("write", lambda *_: self.refresh_list())
        self.sort_var.trace_add("write", lambda *_: self.refresh_list())
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.update_buttons())
        self.tree.bind("<Button-1>", self.on_tree_click, add="+")
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.tree.bind("<Button-3>", self.show_context_menu)
        self.bind("<Return>", lambda _event: self.open_selected())
        self.bind("<Control-f>", self.focus_search)
        self.bind("<Control-F>", self.focus_search)
        self.bind("<Control-n>", lambda _event: self.add_item())
        self.bind("<Control-N>", lambda _event: self.add_item())
        self.bind("<F2>", lambda _event: self.edit_item())
        self.bind("<Delete>", lambda _event: self.delete_item())
        self.bind("<Control-c>", lambda _event: self.copy_paths())
        self.bind("<Control-C>", lambda _event: self.copy_paths())

    def focus_search(self, _event=None):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")
        return "break"

    def selected_item(self):
        selection = self.tree.selection()
        if not selection:
            return None
        item_id = selection[0]
        return next((item for item in self.items if item["id"] == item_id), None)

    def selected_items(self):
        selected_ids = set(self.tree.selection())
        return [item for item in self.items if item.get("id") in selected_ids]

    def on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        column = self.tree.identify_column(event.x)
        item_id = self.tree.identify_row(event.y)
        if item_id:
            if column == "#3":
                self.after_idle(lambda: self.edit_category_inline(item_id))
            elif column == "#5":
                item = next((value for value in self.items if value["id"] == item_id), None)
                if item and not os.path.exists(item.get("path", "")):
                    self.after_idle(lambda: self.repair_path(item))

    def on_tree_double_click(self, event):
        if self.tree.identify_column(event.x) == "#3":
            return "break"
        self.open_selected()

    def edit_category_inline(self, item_id):
        if not self.tree.exists(item_id):
            return
        if self.category_editor is not None:
            self.category_editor.destroy()
        bounds = self.tree.bbox(item_id, "#3")
        if not bounds:
            return
        x, y, width, height = bounds
        item = next((value for value in self.items if value["id"] == item_id), None)
        if not item:
            return
        value = tk.StringVar(value=item.get("category", "未分类"))
        editor = ttk.Combobox(self.tree, textvariable=value, values=self.custom_categories, state="readonly")
        self.category_editor = editor
        editor.place(x=x, y=y, width=width, height=height)
        finished = {"value": False}

        def close(save=False):
            if finished["value"]:
                return
            finished["value"] = True
            if save and value.get() in self.custom_categories:
                item["category"] = value.get()
                save_items(self.items)
            editor.destroy()
            if self.category_editor is editor:
                self.category_editor = None
            if save:
                self.refresh_categories()
                self.refresh_list()

        editor.bind("<<ComboboxSelected>>", lambda _event: close(True))
        editor.bind("<Return>", lambda _event: close(True))
        editor.bind("<Escape>", lambda _event: close(False))
        editor.bind("<FocusOut>", lambda _event: self.after(200, lambda: close(False) if self.focus_get() is not editor else None))
        editor.focus_set()
        editor.event_generate("<Button-1>")

    def get_item_icon(self, item):
        path = item.get("path", "")
        cache_key = os.path.normcase(path)
        if cache_key in self.item_icons:
            return self.item_icons[cache_key]
        try:
            image = extract_shell_icon(path)
            if image is None:
                image = Image.open(resource_path("assets/FolderPilot.ico")).convert("RGBA").resize((24, 24), Image.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except (OSError, ValueError, tk.TclError):
            photo = tk.PhotoImage(width=24, height=24)
        self.item_icons[cache_key] = photo
        return photo

    def refresh_categories(self):
        for button in self.category_buttons:
            button.destroy()
        self.category_buttons.clear()
        categories = ["全部", "★ 已置顶", "◷ 最近使用", "▥ 最常使用"] + self.custom_categories
        if self.category not in categories:
            self.category = "全部"
        for category in categories:
            active = category == self.category
            button = tk.Button(self.categories_frame, text=category, anchor="w", relief="flat", bd=0, cursor="hand2", bg="#e8efff" if active else "white", fg="#315db3" if active else "#354052", activebackground="#edf2fc", font=("Microsoft YaHei UI", 10, "bold" if active else "normal"), padx=11, pady=8, command=lambda value=category: self.set_category(value))
            button.pack(fill="x", pady=1)
            self.category_buttons.append(button)

    def set_category(self, category):
        self.category = category
        self.refresh_categories()
        self.refresh_list()

    def filtered_items(self):
        query = self.search_var.get().strip().casefold()
        result = []
        for item in self.items:
            category_ok = (
                self.category == "全部"
                or (self.category == "★ 已置顶" and item.get("favorite"))
                or (self.category == "◷ 最近使用" and item.get("last_opened_at"))
                or (self.category == "▥ 最常使用" and item.get("open_count", 0) > 0)
                or item.get("category") == self.category
            )
            keywords = item.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [keywords]
            haystack = " ".join((item.get("name", ""), item.get("path", ""), item.get("category", ""), item.get("note", ""), " ".join(keywords))).casefold()
            if category_ok and (not query or query in haystack):
                result.append(item)
        mode = "最近打开" if self.category == "◷ 最近使用" else "使用次数" if self.category == "▥ 最常使用" else self.sort_var.get()
        if mode == "最近打开":
            by_recency = sorted(result, key=lambda x: x.get("last_opened_at", ""), reverse=True)
            return sorted(by_recency, key=lambda x: not x.get("favorite", False))
        if mode == "使用次数":
            return sorted(result, key=lambda x: (not x.get("favorite", False), -x.get("open_count", 0), x.get("name", "").casefold()))
        if mode == "名称排序":
            return sorted(result, key=lambda x: x.get("name", "").casefold())
        return sorted(result, key=lambda x: (not x.get("favorite", False), x.get("name", "").casefold()))

    def refresh_list(self):
        if not hasattr(self, "tree"):
            return
        if self.category_editor is not None:
            self.category_editor.destroy()
            self.category_editor = None
        selected_ids = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        visible = self.filtered_items()
        for item in visible:
            available = os.path.exists(item.get("path", ""))
            self.tree.insert("", "end", iid=item["id"], text="  " + item.get("name", ""), image=self.get_item_icon(item), values=("★" if item.get("favorite") else "", kind_label(item.get("kind", infer_kind(item.get("path", "")))), item.get("category", "未分类"), item.get("path", ""), "可用" if available else "已失效", item.get("open_count", 0)))
        remaining = [item_id for item_id in selected_ids if self.tree.exists(item_id)]
        if remaining:
            self.tree.selection_set(remaining)
        if visible:
            self.empty_label.place_forget()
        else:
            text = "📁\n\n还没有常用项目\n\n点击右上角“添加常用项目”开始整理" if not self.items else "没有符合条件的项目"
            self.empty_label.configure(text=text)
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        self.status_var.set(f"共 {len(self.items)} 个常用项目 · 当前显示 {len(visible)} 个 · 点击分类可快速修改 · 数据仅保存在本机")
        self.update_buttons()

    def update_buttons(self):
        items = self.selected_items()
        state = "normal" if items else "disabled"
        self.open_button.configure(state="normal" if len(items) == 1 else "disabled")
        self.edit_button.configure(state="normal" if len(items) == 1 else "disabled")
        self.delete_button.configure(state=state)
        self.favorite_button.configure(state=state)
        all_favorite = bool(items) and all(item.get("favorite") for item in items)
        self.favorite_button.configure(text="★ 取消置顶" if all_favorite else "☆ 置顶")

    def manage_categories(self):
        dialog = CategoryDialog(self)
        self.wait_window(dialog)

    def category_names(self):
        return list(self.custom_categories)

    def add_item(self):
        dialog = FolderDialog(self, self.category_names())
        self.wait_window(dialog)
        if not dialog.result:
            return
        if any(os.path.normcase(item.get("path", "")) == os.path.normcase(dialog.result["path"]) for item in self.items):
            messagebox.showinfo(APP_NAME, "这个项目已经添加过了。", parent=self)
            return
        self.items.append({"id": uuid4().hex, **dialog.result, "favorite": False, "open_count": 0, "created_at": datetime.now().isoformat(), "last_opened_at": ""})
        if dialog.result["category"] not in self.custom_categories:
            self.custom_categories.append(dialog.result["category"])
            save_categories(self.custom_categories)
        save_items(self.items)
        self.refresh_categories()
        self.refresh_list()

    def edit_item(self):
        item = self.selected_item()
        if not item:
            return
        dialog = FolderDialog(self, self.category_names(), item)
        self.wait_window(dialog)
        if not dialog.result:
            return
        duplicate = any(other["id"] != item["id"] and os.path.normcase(other.get("path", "")) == os.path.normcase(dialog.result["path"]) for other in self.items)
        if duplicate:
            messagebox.showinfo(APP_NAME, "这个项目已经添加过了。", parent=self)
            return
        item.update(dialog.result)
        if dialog.result["category"] not in self.custom_categories:
            self.custom_categories.append(dialog.result["category"])
            save_categories(self.custom_categories)
        save_items(self.items)
        self.refresh_categories()
        self.refresh_list()

    def delete_item(self):
        items = self.selected_items()
        if not items:
            return
        label = f"「{items[0]['name']}」" if len(items) == 1 else f"选中的 {len(items)} 个项目"
        if messagebox.askyesno(APP_NAME, f"确定从列表中移除{label}吗？\n\n不会删除真实文件或文件夹。", parent=self):
            for item in items:
                self.items.remove(item)
            save_items(self.items)
            self.refresh_categories()
            self.refresh_list()

    def toggle_favorite(self):
        items = self.selected_items()
        if items:
            new_value = not all(item.get("favorite", False) for item in items)
            for item in items:
                item["favorite"] = new_value
            save_items(self.items)
            self.refresh_list()

    def open_selected(self):
        item = self.selected_item()
        if item:
            self.open_item(item)

    def open_item(self, item):
        path = item.get("path", "")
        if not os.path.exists(path):
            if messagebox.askyesno(APP_NAME, f"找不到该项目，可能已被移动或删除：\n\n{path}\n\n是否现在修复路径？", parent=self):
                self.repair_path(item)
            return
        kind = item.get("kind") or infer_kind(path)
        if kind == "script" and not item.get("trusted", False):
            dialog = ScriptConfirmDialog(self, item)
            self.wait_window(dialog)
            if not dialog.result:
                return
            if dialog.trust.get():
                item["trusted"] = True
                save_items(self.items)
        extension = Path(path).suffix.lower()
        try:
            if kind == "folder":
                subprocess.Popen(["explorer.exe", path])
            elif kind == "script" and extension == ".ps1":
                subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path])
            elif kind == "script" and extension in {".vbs", ".js", ".wsf"}:
                subprocess.Popen(["wscript.exe", path])
            else:
                os.startfile(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法打开该项目：\n\n{exc}", parent=self)
            return
        item["open_count"] = item.get("open_count", 0) + 1
        item["last_opened_at"] = datetime.now().isoformat()
        save_items(self.items)
        self.refresh_list()

    def repair_path(self, item=None):
        item = item or self.selected_item()
        if not item:
            return
        kind = item.get("kind") or infer_kind(item.get("path", ""))
        if kind == "folder":
            new_path = filedialog.askdirectory(parent=self, title="重新选择文件夹")
        else:
            new_path = filedialog.askopenfilename(parent=self, title="重新选择文件或脚本", filetypes=(("所有文件", "*.*"),))
        if not new_path:
            return
        normalized = os.path.normpath(new_path)
        duplicate = any(other["id"] != item["id"] and os.path.normcase(other.get("path", "")) == os.path.normcase(normalized) for other in self.items)
        if duplicate:
            messagebox.showinfo(APP_NAME, "这个项目已经添加过了。", parent=self)
            return
        self.item_icons.pop(os.path.normcase(item.get("path", "")), None)
        item["path"] = normalized
        item["kind"] = infer_kind(normalized) if kind != "script" else "script"
        item["trusted"] = False
        save_items(self.items)
        self.refresh_list()

    def open_location(self):
        item = self.selected_item()
        if not item:
            return
        path = item.get("path", "")
        if not os.path.exists(path):
            self.repair_path(item)
        elif os.path.isdir(path):
            subprocess.Popen(["explorer.exe", path])
        else:
            subprocess.Popen(["explorer.exe", f"/select,{path}"])

    def copy_paths(self):
        items = self.selected_items()
        if not items:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(item.get("path", "") for item in items))
        self.status_var.set(f"已复制 {len(items)} 个路径")

    def change_category(self, category):
        items = self.selected_items()
        if not items:
            return
        for item in items:
            item["category"] = category
        save_items(self.items)
        self.refresh_categories()
        self.refresh_list()

    def show_context_menu(self, event):
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        if item_id not in self.tree.selection():
            self.tree.selection_set(item_id)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="打开", command=self.open_selected, state="normal" if len(self.selected_items()) == 1 else "disabled")
        menu.add_command(label="打开所在文件夹", command=self.open_location, state="normal" if len(self.selected_items()) == 1 else "disabled")
        menu.add_command(label="复制路径", command=self.copy_paths)
        category_menu = tk.Menu(menu, tearoff=False)
        for category in self.custom_categories:
            category_menu.add_command(label=category, command=lambda value=category: self.change_category(value))
        menu.add_cascade(label="修改分类", menu=category_menu)
        menu.add_separator()
        menu.add_command(label="置顶 / 取消置顶", command=self.toggle_favorite)
        menu.add_command(label="修复路径", command=self.repair_path, state="normal" if len(self.selected_items()) == 1 else "disabled")
        menu.add_command(label="编辑", command=self.edit_item, state="normal" if len(self.selected_items()) == 1 else "disabled")
        menu.add_command(label="删除收藏", command=self.delete_item)
        menu.tk_popup(event.x_root, event.y_root)

    def add_dropped_paths(self, paths):
        existing = {os.path.normcase(item.get("path", "")) for item in self.items}
        added = 0
        for path in paths:
            normalized = os.path.normpath(path)
            if not os.path.exists(normalized) or os.path.normcase(normalized) in existing:
                continue
            kind = infer_kind(normalized)
            self.items.append({
                "id": uuid4().hex, "name": Path(normalized).name or normalized,
                "path": normalized, "category": "未分类", "note": "", "keywords": [],
                "kind": kind, "favorite": False, "trusted": False, "open_count": 0,
                "created_at": datetime.now().isoformat(), "last_opened_at": "",
            })
            existing.add(os.path.normcase(normalized))
            added += 1
        if added:
            save_items(self.items)
            self.refresh_categories()
            self.refresh_list()
            self.status_var.set(f"已通过拖放添加 {added} 个常用项目")

    def export_data(self):
        path = filedialog.asksaveasfilename(parent=self, title="导出收藏数据", defaultextension=".json", initialfile=f"FolderPilot-backup-{datetime.now():%Y%m%d}.json", filetypes=(("JSON 备份", "*.json"),))
        if not path:
            return
        payload = {"version": 1, "exported_at": datetime.now().isoformat(), "items": self.items, "categories": self.custom_categories}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        messagebox.showinfo(APP_NAME, "数据导出成功。", parent=self)

    def import_data(self):
        path = filedialog.askopenfilename(parent=self, title="导入收藏数据", filetypes=(("JSON 备份", "*.json"), ("所有文件", "*.*")))
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            incoming = payload.get("items")
            categories = payload.get("categories", [])
            if not isinstance(incoming, list) or not isinstance(categories, list):
                raise ValueError("备份文件结构不正确")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"无法导入备份：\n\n{exc}", parent=self)
            return
        choice = messagebox.askyesnocancel(APP_NAME, "选择导入方式：\n\n“是”＝与现有数据合并\n“否”＝替换现有数据\n“取消”＝不导入", parent=self)
        if choice is None:
            return
        if choice:
            known = {os.path.normcase(item.get("path", "")) for item in self.items}
            for item in incoming:
                if os.path.normcase(item.get("path", "")) not in known:
                    item["id"] = item.get("id") or uuid4().hex
                    self.items.append(item)
                    known.add(os.path.normcase(item.get("path", "")))
        else:
            self.items = incoming
        self.custom_categories = list(dict.fromkeys(self.custom_categories + categories + [item.get("category", "未分类") for item in self.items])) if choice else list(dict.fromkeys(categories + [item.get("category", "未分类") for item in self.items]))
        if "未分类" not in self.custom_categories:
            self.custom_categories.append("未分类")
        save_items(self.items)
        save_categories(self.custom_categories)
        self.item_icons.clear()
        self.refresh_categories()
        self.refresh_list()
        messagebox.showinfo(APP_NAME, "数据导入成功。", parent=self)

    def show_data_menu(self):
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="导出备份…", command=self.export_data)
        menu.add_command(label="导入备份…", command=self.import_data)
        menu.add_command(label="恢复最近一次自动备份", command=self.restore_latest_backup)
        menu.add_separator()
        menu.add_command(label="打开数据目录", command=self.open_data_directory)
        x, y = self.winfo_pointerxy()
        menu.tk_popup(x, y)

    def restore_latest_backup(self):
        backups = sorted(BACKUP_DIR.glob("backup-*.json"), reverse=True) if BACKUP_DIR.exists() else []
        if not backups:
            messagebox.showinfo(APP_NAME, "目前没有可恢复的自动备份。", parent=self)
            return
        latest = backups[0]
        if not messagebox.askyesno(APP_NAME, f"是否恢复最近一次自动备份？\n\n{latest.name}\n\n当前数据会先自动备份。", parent=self):
            return
        try:
            payload = json.loads(latest.read_text(encoding="utf-8"))
            incoming = payload["items"]
            categories = payload.get("categories", [])
            if not isinstance(incoming, list) or not isinstance(categories, list):
                raise ValueError("备份文件结构不正确")
            self.items = incoming
            self.custom_categories = list(dict.fromkeys(categories + [item.get("category", "未分类") for item in incoming]))
            if "未分类" not in self.custom_categories:
                self.custom_categories.append("未分类")
            save_items(self.items)
            save_categories(self.custom_categories)
            self.item_icons.clear()
            self.refresh_categories()
            self.refresh_list()
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"自动备份恢复失败：\n\n{exc}", parent=self)

    def open_data_directory(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(DATA_DIR)

    def show_help(self):
        dialog = tk.Toplevel(self)
        dialog.title("帮助与快捷键")
        dialog.geometry("620x560")
        dialog.transient(self)
        body = ttk.Frame(dialog, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="文件夹管家使用帮助", font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        help_text = (
            "常用操作\n"
            "• 将文件夹、文件或脚本直接拖入主列表即可添加\n"
            "• 点击分类单元格可直接更换分类\n"
            "• 按住 Ctrl 或 Shift 可多选项目，批量分类、置顶或删除\n"
            "• 右键项目可打开所在位置、复制路径或修复失效路径\n"
            "• 关闭窗口后软件进入系统托盘，右键托盘图标可退出\n\n"
            "键盘快捷键\n"
            "Ctrl + Alt + Space    全局呼出并搜索\n"
            "Ctrl + F              搜索\n"
            "Ctrl + N              添加常用项目\n"
            "Ctrl + C              复制所选路径\n"
            "F2                    编辑所选项目\n"
            "Delete                删除所选收藏\n"
            "↑ / ↓                 选择项目\n"
            "Enter                 打开所选项目\n\n"
            "数据与安全\n"
            "• 每次修改会自动保留最近 10 份备份\n"
            "• “备份 / 恢复”可导出或导入完整数据\n"
            "• 启动脚本首次运行时需要确认，可单独设为信任"
        )
        text = tk.Text(body, wrap="word", relief="flat", bg="white", fg="#273043", font=("Microsoft YaHei UI", 10), padx=16, pady=14, spacing1=4, spacing3=4)
        text.insert("1.0", help_text)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, pady=(15, 12))
        ttk.Button(body, text="关闭", command=dialog.destroy).pack(anchor="e")

    def save_window_settings(self):
        widths = {column: self.tree.column(column, "width") for column in ("#0", "star", "kind", "category", "path", "status", "count")}
        save_settings({"geometry": self.geometry(), "sort": self.sort_var.get(), "category": self.category, "widths": widths})

    def hide_to_tray(self):
        self.save_window_settings()
        self.withdraw()

    def show_window(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def show_and_search(self):
        self.show_window()
        self.focus_search()

    def exit_application(self):
        self.save_window_settings()
        if hasattr(self, "windows"):
            self.windows.cleanup()
        if hasattr(self, "instance_socket"):
            try:
                self.instance_socket.close()
            except OSError:
                pass
        self.destroy()


def acquire_single_instance():
    instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        instance_socket.bind(("127.0.0.1", 49327))
        instance_socket.listen(2)
        return instance_socket
    except OSError:
        instance_socket.close()
        try:
            notifier = socket.create_connection(("127.0.0.1", 49327), timeout=1)
            notifier.sendall(b"SHOW")
            notifier.close()
        except OSError:
            pass
        return None


def listen_for_existing_launch(app, instance_socket):
    while True:
        try:
            connection, _address = instance_socket.accept()
            with connection:
                if connection.recv(16) == b"SHOW":
                    app.after(0, app.show_window)
        except OSError:
            return


def enable_high_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--check", action="store_true", help="只检查运行环境")
    args = parser.parse_args()
    if args.check:
        print(f"{APP_NAME}: Python {sys.version.split()[0]}, Tk {tk.TkVersion}, 环境检查通过")
        return
    instance_socket = acquire_single_instance()
    if instance_socket is None:
        return
    enable_high_dpi()
    app = FolderPilot()
    app.instance_socket = instance_socket
    threading.Thread(target=listen_for_existing_launch, args=(app, instance_socket), daemon=True).start()
    app.mainloop()


if __name__ == "__main__":
    main()
