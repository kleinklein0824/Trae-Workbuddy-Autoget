#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""页面集合。"""
from .accounts import AccountsPage
from .api_service import ApiServicePage
from .checkin import CheckinPage
from .credits import CreditsPage
from .dashboard import DashboardPage
from .logs import LogsPage
from .settings import SettingsPage

__all__ = ["DashboardPage", "CheckinPage", "AccountsPage", "CreditsPage",
           "ApiServicePage", "LogsPage", "SettingsPage"]
