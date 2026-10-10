# ----------------------------------------------------------------- footer --
    def _build_footer(self) -> None:
        # MSFluentWindow does not expose the root layout reliably, so instead
        # each page lays out its own bottom row via _add_footer_bar().
        pass

    def update_icon(self, icon_path: str) -> None:
        """
        Update the instances icons with the new icon.
        """
        for instance in self.instances:
            instance.set_icon(icon_path)
        self.refresh_instances()
    def _add_footer_bar(self, page_layout) -> None:
        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = PushButton(FluentIcon.CLOSE, "Cancel")
    def refresh_instances(self) -> None:
        """
        Refresh the instances icons.
        """
        self.instances_list.clear()
        for instance in self.instances:
            self.instances_list.addItem(instance.name)
        save = PushButton(FluentIcon.SAVE, "Save")
        save.setObjectName("primaryBtn")
        bar.addWidget(cancel)
        bar.addWidget(save)
        page_layout.addLayout(bar)
        cancel.clicked.connect(self.close)
    def _apply_and_close(self) -> None:
        """
        Apply the changes and close the window.
        """
        self.apply_changes()
        self.update_icon(self.icon_path)
        self.close()
        save.clicked.connect(lambda: (self._apply_and_close(), self.update_icon(self.icon_path)))