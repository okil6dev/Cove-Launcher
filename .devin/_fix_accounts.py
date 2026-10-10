import pathlib, re

p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launcher/pages/accounts_page.py')
s = p.read_text(encoding='utf-8')

# Fix 1: _on_add_offline - parent dialog to window(), defer to idle, defer refresh
old_add_offline = '''    def _on_add_offline(self) -> None:
        dialog = _OfflineDialog(self)
        if dialog.exec():
            account = self._store.add_offline(dialog.name_edit.text())
            self._log(f"Added offline account '{account.name}'.")
            self.refresh()'''

new_add_offline = '''    def _on_add_offline(self) -> None:
        from PyQt6.QtCore import QTimer

        def _perform() -> None:
            # Parent to self.window() (top-level sibling) so the Fluent fade-in
            # doesn't grab a pixmap of the list view mid-repaint.
            dialog = _OfflineDialog(self.window())
            if dialog.exec():
                account = self._store.add_offline(dialog.name_edit.text())
                self._log(f"Added offline account '{account.name}'.")
                # Defer the refresh 60 ms so the dialog fade-out doesn't race
                # with the list rebuild.
                QTimer.singleShot(60, self.refresh)

        # Defer to idle so the click cycle completes before the modal opens.
        QTimer.singleShot(0, _perform)'''

if old_add_offline in s:
    s = s.replace(old_add_offline, new_add_offline)
    print("FIXED: _on_add_offline")
else:
    print("SKIP: _on_add_offline (not found)")

# Fix 2: _on_login_ok - defer refresh
old_login_ok = '''        self._log(f"Logged in as {account.name} via Microsoft.")
        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)
        self.refresh()'''

new_login_ok = '''        self._log(f"Logged in as {account.name} via Microsoft.")
        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)
        # Defer the refresh so the toast fade-out doesn't race with the
        # list rebuild.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(60, self.refresh)'''

if old_login_ok in s:
    s = s.replace(old_login_ok, new_login_ok)
    print("FIXED: _on_login_ok")
else:
    print("SKIP: _on_login_ok (not found)")

# Fix 3: _on_ely_ok - defer refresh
old_ely_ok_search = '        self._log(f"Logged in as {account.name} via Ely.by.")\n        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)\n        self.refresh()'
new_ely_ok_search = '''        self._log(f"Logged in as {account.name} via Ely.by.")
        self._toast("success", "Logged in", f"Welcome, {account.name}!", 5000)
        # Defer the refresh so the toast fade-out doesn't race with the
        # list rebuild.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(60, self.refresh)'''

if old_ely_ok_search in s:
    s = s.replace(old_ely_ok_search, new_ely_ok_search)
    print("FIXED: _on_ely_ok")
else:
    print("SKIP: _on_ely_ok (not found)")

# Fix 4: _on_set_current - defer refresh
old_set_current = '''            self._store.set_current(uuid_str)
            self.refresh()
            self._log("Active account changed.")'''

new_set_current = '''            self._store.set_current(uuid_str)
            self._log("Active account changed.")
            # Defer the refresh so the button's click cycle completes first.
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(60, self.refresh)'''

if old_set_current in s:
    s = s.replace(old_set_current, new_set_current)
    print("FIXED: _on_set_current")
else:
    print("SKIP: _on_set_current (not found)")

p.write_text(s, encoding='utf-8')
print("\nDone. File written.")
