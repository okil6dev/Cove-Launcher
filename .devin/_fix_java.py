import pathlib

p = pathlib.Path('C:/Users/minec/AppData/Apps/MinecraftLauncher/launcher/pages/java_page.py')
s = p.read_text(encoding='utf-8')

old = '''        def _perform() -> None:
            # Parent to the *window* so the dialog is a sibling of the card,
            # not a child — keeps the Fluent fade-in (QWidgetEffectSourcePrivate)
            # from grabbing a pixmap of the card mid-repaint.
            box = MessageBox(
                "Delete Java runtime?",
                f"Remove {fname} Java {ver} from disk?\\n\\n{folder}\\n\\n"
                "Instances pointing at this runtime will fall back to "
                "Mojang's bundled JVM on their next launch.",
                self.window(),
            )
            box.yesButton.setText("Delete")
            box.cancelButton.setText("Keep")
            if not box.exec():
                return

            import shutil

            try:
                shutil.rmtree(folder)
            except OSError as exc:
                self._toast(
                    "error", "Delete failed",
                    f"{str(exc)[:160]}\\n(The JVM may still be running.)", 8000,
                )
                return
            # A deleted GraalVM must vanish from cached launch-time detection.
            try:
                from ..graalvm import detect_graalvm
                detect_graalvm.cache_clear()
            except Exception:  # noqa: BLE001 - cache reset is best-effort
                pass
            self._log(f"Deleted Java runtime: {folder}")
            self._toast("success", "Java removed",
                        f"{fname} Java {ver} deleted.", 5000)
            # Defer the refresh 60 ms so the dialog fade-out doesn't race with
            # the card repaint.
            QTimer.singleShot(60, self._refresh_install_status)'''

new = '''        def _perform() -> None:
            # Hide the card area so the dialog's Fluent fade effect can't grab
            # a pixmap of it mid-repaint (which causes "QPainter not active").
            self.setVisible(False)
            try:
                # Parent to the *window* so the dialog is a sibling of the
                # card, not a child.
                box = MessageBox(
                    "Delete Java runtime?",
                    f"Remove {fname} Java {ver} from disk?\\n\\n{folder}\\n\\n"
                    "Instances pointing at this runtime will fall back to "
                    "Mojang's bundled JVM on their next launch.",
                    self.window(),
                )
                box.yesButton.setText("Delete")
                box.cancelButton.setText("Keep")
                if not box.exec():
                    return

                import shutil

                try:
                    shutil.rmtree(folder)
                except OSError as exc:
                    self._toast(
                        "error", "Delete failed",
                        f"{str(exc)[:160]}\\n(The JVM may still be running.)", 8000,
                    )
                    return
                # A deleted GraalVM must vanish from cached launch-time detection.
                try:
                    from ..graalvm import detect_graalvm
                    detect_graalvm.cache_clear()
                except Exception:  # noqa: BLE001 - cache reset is best-effort
                    pass
                self._log(f"Deleted Java runtime: {folder}")
                self._toast("success", "Java removed",
                            f"{fname} Java {ver} deleted.", 5000)
                self._refresh_install_status()
            finally:
                # Always re-show the card, even if refresh raised.
                self.setVisible(True)'''

if old in s:
    s = s.replace(old, new)
    p.write_text(s, encoding='utf-8')
    print("FIXED: java_page._on_delete")
else:
    print("SKIP: java_page")
