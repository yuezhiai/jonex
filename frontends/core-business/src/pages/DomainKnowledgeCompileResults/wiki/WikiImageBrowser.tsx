import React, { useRef, useState } from 'react';
import { Alert, Button, Drawer, Input, Space, Spin } from 'antd';
import { PictureOutlined } from '@ant-design/icons';
import { findWikiImages, inspectWikiImage, type WikiImageSearch } from '@/api/wikiImages';
import type { WikiSourceImage } from '@/types/domainKnowledge';
import WikiSourcePicture from '@/components/WikiSourcePicture';
import { useDocumentViewer, inferMediaType } from '@/components/DocumentViewer';

export default function WikiImageBrowser({ kbId, documentId = '' }: { kbId: string; documentId?: string }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<WikiImageSearch | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const { openDocument, viewer } = useDocumentViewer();
  React.useEffect(() => {
    sequence.current++;
    setResult(null);
    setError('');
    setLoading(false);
    setOpen(false);
  }, [kbId, documentId]);
  const search = async (more = false) => {
    const token = ++sequence.current;
    setLoading(true);
    setError('');
    try {
      const next = await findWikiImages(kbId, {
        query,
        ...(documentId ? { document_id: documentId } : {}),
        ...(more && result ? { revision: result.revision, cursor: result.cursor } : {}),
      });
      if (token === sequence.current)
        setResult(more && result ? { ...next, items: [...result.items, ...next.items] } : next);
    } catch (e) {
      if (token === sequence.current) setError(e instanceof Error ? e.message : '图片检索失败');
    } finally {
      if (token === sequence.current) setLoading(false);
    }
  };
  return (
    <>
      <Button icon={<PictureOutlined />} onClick={() => setOpen(true)}>
        找图
      </Button>
      <Drawer
        title="原文图片"
        open={open}
        onClose={() => {
          sequence.current++;
          setOpen(false);
          setLoading(false);
        }}
        size="large"
        destroyOnHidden
      >
        <Input.Search
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setResult(null);
            sequence.current++;
            setLoading(false);
          }}
          placeholder="按图注或原文内容查找，留空浏览图片"
          onSearch={() => search()}
          enterButton="查找"
          loading={loading}
          maxLength={512}
        />
        <p style={{ color: '#64748b', fontSize: 12 }}>
          使用已有图注、OCR 和原文线索。附近文字仅作候选；没有文字线索的图片可能无法检出。
        </p>
        {error && <Alert type="error" title={error} showIcon />}
        {result && (
          <p>
            找到 {result.total} 张
            {result.coverage === 'image_index_unavailable' ? '；当前发布版本尚无图片索引，需要重新编译' : ''}
          </p>
        )}
        {result?.items.map((image) => (
          <div
            key={`${image.bundle_sha}:${image.asset_id}`}
            style={{ borderBottom: '1px solid #e2e8f0', paddingBottom: 16 }}
          >
            <WikiSourcePicture
              image={image}
              onOpenSource={(item) =>
                openDocument({
                  docId: item.document_id,
                  fileName: item.source_name,
                  mediaType: inferMediaType(item.source_name),
                })
              }
            />
            <ReadPicture key={`${image.bundle_sha}:${image.asset_id}`} kbId={kbId} image={image} />
          </div>
        ))}
        {loading && <Spin />}
        {result?.has_more && (
          <Button onClick={() => search(true)} disabled={loading}>
            加载更多
          </Button>
        )}
      </Drawer>
      {viewer}
    </>
  );
}

function ReadPicture({ kbId, image }: { kbId: string; image: WikiSourceImage }) {
  const [question, setQuestion] = useState('');
  const [reading, setReading] = useState(false);
  const [result, setResult] = useState<Awaited<ReturnType<typeof inspectWikiImage>> | null>(null);
  const [error, setError] = useState('');
  const ask = async () => {
    setReading(true);
    setError('');
    setResult(null);
    try {
      setResult(await inspectWikiImage(kbId, image, question));
    } catch (e) {
      setError(e instanceof Error ? e.message : '读图失败');
    } finally {
      setReading(false);
    }
  };
  return (
    <>
      <Space.Compact style={{ width: '100%' }}>
        <Input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="需要理解画面时，输入关于这张图的问题"
          maxLength={1000}
          disabled={reading}
        />
        <Button onClick={ask} disabled={!question.trim()} loading={reading}>
          读图
        </Button>
      </Space.Compact>
      {error && <Alert type="warning" title={error} />}
      {result?.actual_image_delivered && (
        <div style={{ whiteSpace: 'pre-wrap', marginTop: 12 }}>
          {result.result.observations}
          <br />
          {result.result.visible_text}
          {result.result.uncertainties.map((line, i) => (
            <p key={i} style={{ color: '#ad6800' }}>
              {line}
            </p>
          ))}
          <small style={{ color: '#64748b' }}>
            已读取当前图片{result.cached ? '（复用之前的同图同问题记录）' : ''} · {result.read_id}
          </small>
        </div>
      )}
    </>
  );
}
