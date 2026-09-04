#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""Repositories for DomainService and related models."""

from jonex_core.common.repository import BaseRepository

from ..models.domain_service import (
    DomainService,
    ServiceApiKey,
    ServiceKnowledgeBase,
)


class DomainServiceRepository(BaseRepository[DomainService]):
    model = DomainService


class ServiceKnowledgeBaseRepository(BaseRepository[ServiceKnowledgeBase]):
    model = ServiceKnowledgeBase


class ServiceApiKeyRepository(BaseRepository[ServiceApiKey]):
    model = ServiceApiKey
