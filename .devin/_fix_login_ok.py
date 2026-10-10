import pathlib

p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launcher/pages/accounts_page.py')
s = p.read_text(encoding='utf-8')

# Fix _on_login_ok - use setUpdatesEnabled
old_login = '''        self._log(f"Logged in as {account.name} via Microsoft.")
        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)
        # Defer the refresh so the toast fade-out doesn't race with the
        # list rebuild.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(60, self.refresh)'''

new_login = '''        self._log(f"Logged in as {account.name} via Microsoft.")
        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)
        # Block list updates during refresh so the toast fade-out can't race
        # with the list rebuild.
        self.list_widget.setUpdatesEnabled(False)
        try:
            self.refresh()
        finally:
            self.list_widget.setUpdatesEnabled(True)'''

if old_login in s:
    s = s.replace(old_login, new_login)
    print("FIXED: _on_login_ok")
else:
    print("SKIP: _on_login_ok")

# Fix _on_ely_ok - use setUpdatesEnabled
old_ely = '''        self._log(f"Logged in as {account.name} via Ely.by.")
        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)
        # Defer the refresh so the toast fade-out doesn't race with the
        # list rebuild.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(60, self.refresh)'''

new_ely = '''        self._log(f"Logged in as {account.name} via Ely.by.")
        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)
        # Block list updates during refresh so the toast fade-out can't race
        # with the list rebuild.
        self.list_widget.setUpdatesEnabled(False)
        try:
            self.refresh()
        finally:
            self.list_widget.setUpdatesEnabled(True)'''

if old_ely in s:
    s = s.replace(old_ely, new_ely)
    print("FIXED: _on_ely_ok")
else:
    print("SKIP: _on_ely_ok")

p.write_text(s, encoding='utf-8')
print("Done.")
