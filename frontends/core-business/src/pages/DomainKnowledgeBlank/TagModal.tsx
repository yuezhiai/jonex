import React, { useEffect, useState } from 'react';
import { Modal, Input, Button, Tag, Space, message, Spin } from 'antd';
import { CloseOutlined, PlusOutlined, SaveOutlined, TagOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getKnowledgeBaseTags, createKnowledgeBaseTag, getDocumentTags, setDocumentTags } from '@/api/domainKnowledge';
import type { TagItem } from '@/api/domainKnowledge';
import './index.scss';

const TAG_COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4', '#f97316'];

interface TagModalProps {
  open: boolean;
  kbId: string;
  docId?: string;
  docName?: string;
  onClose: () => void;
}

export default function TagModal({ open, kbId, docId, docName, onClose }: TagModalProps) {
  const { t } = useTranslation();
  const [selectedTags, setSelectedTags] = useState<TagItem[]>([]);
  const [commonTags, setCommonTags] = useState<TagItem[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open && docId) {
      setInputValue('');
      setLoading(true);
      Promise.all([getDocumentTags(docId, kbId), getKnowledgeBaseTags(kbId)])
        .then(([docTags, kbTags]) => {
          setSelectedTags(docTags);
          setCommonTags(kbTags);
        })
        .catch((err: any) => {
          message.error(err?.message || t('common.loadFailed'));
        })
        .finally(() => setLoading(false));
    }
  }, [open, docId, kbId]);

  const handleAdd = async () => {
    const val = inputValue.trim();
    if (!val) return;
    if (selectedTags.some((t) => t.name === val)) {
      message.warning(t('common.tagExists'));
      return;
    }

    // 先创建标签
    try {
      const randomColor = TAG_COLORS[Math.floor(Math.random() * TAG_COLORS.length)];
      const newTag = await createKnowledgeBaseTag({
        knowledge_base_id: kbId,
        name: val,
        color: randomColor,
      });
      // 创建成功后选中
      setSelectedTags((prev) => [...prev, newTag]);
      setCommonTags((prev) => [...prev, newTag]);
      setInputValue('');
      message.success(t('common.tagAdded'));
    } catch (err: any) {
      message.error(err?.message || t('common.tagCreateFailed'));
    }
  };

  const handleRemove = (tagId: string) => {
    setSelectedTags((prev) => prev.filter((t) => t.id !== tagId));
  };

  const handleCommonTagClick = (tag: TagItem) => {
    if (selectedTags.some((t) => t.id === tag.id)) {
      // 已选中则取消
      handleRemove(tag.id);
      return;
    }
    setSelectedTags((prev) => [...prev, tag]);
  };

  const handlePressEnter = (e: React.KeyboardEvent<HTMLInputElement>) => {
    e.preventDefault();
    handleAdd();
  };

  const handleSave = async () => {
    if (!docId) return;
    setSaving(true);
    try {
      await setDocumentTags(docId, {
        knowledge_base_id: kbId,
        tag_ids: selectedTags.map((t) => t.id),
      });
      message.success(t('common.saveSuccess'));
      onClose();
    } catch (err: any) {
      message.error(err?.message || t('common.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title={
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 18, color: '#0b2b5c' }}>
          <TagOutlined style={{ color: '#3b82f6', fontSize: 22 }} />
          {t('common.setTags')}
        </div>
      }
      closeIcon={<CloseOutlined style={{ fontSize: 16, color: '#475569' }} />}
      onCancel={onClose}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
          <Button onClick={onClose} disabled={saving}>
            {t('common.cancel')}
          </Button>
          <Button type="primary" icon={<SaveOutlined />} onClick={handleSave} loading={saving}>
            {t('common.save')}
          </Button>
        </div>
      }
      width={560}
    >
      <Spin spinning={loading}>
        <div className="tag-modal-content">
          <div className="tag-modal-doc">
            {t('common.documentLabel')}
            <span>{docName}</span>
          </div>

          <div className="tag-modal-tags">
            {selectedTags.map((tag) => (
              <Tag
                key={tag.id}
                closable
                onClose={() => handleRemove(tag.id)}
                closeIcon={<CloseOutlined style={{ fontSize: 10, color: tag.color }} />}
                color={tag.color}
                className="tag-modal-tag-item"
              >
                {tag.name}
              </Tag>
            ))}
          </div>

          <div className="tag-modal-input-row">
            <Input
              placeholder={t('common.tagInputPlaceholder')}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onPressEnter={handlePressEnter}
              className="tag-modal-input"
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd} className="tag-modal-add-btn">
              {t('common.addNew')}
            </Button>
          </div>

          <div className="tag-modal-common">
            <div className="tag-modal-common-title">{t('common.commonTags')}</div>
            <Space size={8} wrap>
              {commonTags.map((tag) => {
                const isActive = selectedTags.some((t) => t.id === tag.id);
                return (
                  <Tag
                    key={tag.id}
                    color={isActive ? tag.color : 'default'}
                    className={`tag-modal-common-item ${isActive ? 'tag-modal-common-item--active' : ''}`}
                    onClick={() => handleCommonTagClick(tag)}
                  >
                    {tag.name}
                  </Tag>
                );
              })}
            </Space>
          </div>
        </div>
      </Spin>
    </Modal>
  );
}
