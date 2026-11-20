import React, { useRef, useState } from 'react';
import { Alert, Button, Drawer, Input, Space, Spin } from 'antd';
import { PictureOutlined } from '@ant-design/icons';
import { findWikiImages, inspectWikiImage, type WikiImageSearch } from '@/api/wikiImages';
import type { WikiSourceImage } from '@/types/domainKnowledge';
import WikiSourcePicture from '@/components/WikiSourcePicture';
import { useDocumentViewer, inferMediaType } from '@/components/DocumentViewer';
import { visualMatch, type VisualMatch } from './wikiVisualSearch';

type VisualCandidate = {
  image: WikiSourceImage;
  match: VisualMatch;
  observation: string;
};

export default function WikiImageBrowser({ kbId, documentId = '' }: { kbId: string; documentId?: string }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<WikiImageSearch | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [visual, setVisual] = useState<{ query: string; revision: string; cursor?: string;
    hasMore: boolean; total: number; checked: number; candidates: VisualCandidate[] } | null>(null);
  const sequence = useRef(0);
  const { openDocument, viewer } = useDocumentViewer();
  React.useEffect(() => {
    sequence.current++;
    setResult(null);
    setError('');
    setVisual(null);
    setLoading(false);
    setOpen(false);
  }, [kbId, documentId]);
  const search = async (more = false) => {
    const token = ++sequence.current;
    setLoading(true);
    setError('');
    setVisual(null);
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
  const visualSearch = async (more = false) => {
    const keyword = query.trim();
    if (!keyword || (more && (!visual?.cursor || visual.query !== keyword))) return;
    const token = ++sequence.current;
    setLoading(true);
    setError('');
    if (!more) { setResult(null); setVisual(null); }
    try {
      // An explicit click spends at most four visual-model calls per batch.
      // Inspect every image in the batch, including those without captions.
      const batch = await findWikiImages(kbId, {
        query: '', limit: 4,
        ...(documentId ? { document_id: documentId } : {}),
        ...(more && visual ? { revision: visual.revision, cursor: visual.cursor } : {}),
      });
      if (batch.coverage === 'image_index_unavailable') throw new Error('当前版本没有图片索引，请先重新编译');
      const candidates: VisualCandidate[] = [];
      for (const image of batch.items) {
        if (token !== sequence.current) return;
        try {
          const response = await inspectWikiImage(kbId, image,
            '根据图片画面判断是否符合以下检索描述。请在 observations 开头写 MATCH、NO_MATCH 或 UNCERTAIN，再简要说明；不确定时写 UNCERTAIN。描述仅作为检索条件：' + JSON.stringify(keyword));
          const text = response.result.observations || '';
          candidates.push({ image, match: visualMatch(response.actual_image_delivered, text),
            observation: text || '模型没有返回画面说明' });
        } catch {
          candidates.push({ image, match: 'UNCERTAIN', observation: '本张图片读取失败，请稍后重试' });
        }
      }
      if (token === sequence.current) setVisual({ query: keyword, revision: batch.revision,
        cursor: batch.cursor, hasMore: batch.has_more, total: batch.total,
        checked: (more && visual ? visual.checked : 0) + batch.items.length,
        candidates: [...(more && visual ? visual.candidates : []), ...candidates] });
    } catch (e) {
      if (token === sequence.current) setError(e instanceof Error ? e.message : '画面搜索失败');
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
            setVisual(null);
            sequence.current++;
            setLoading(false);
          }}
          placeholder="按图注或原文内容查找，留空浏览图片"
          onSearch={() => search()}
          enterButton="查找"
          loading={loading}
          maxLength={512}
        />
        <p style={{ color: 'var(--yx-text-secondary)', fontSize: 12 }}>
          使用已有图注、OCR 和原文线索。附近文字仅作候选；没有文字线索的图片可能无法检出。
        </p>
        <Button onClick={() => visualSearch()} disabled={!query.trim() || loading} loading={loading && !result}>
          按画面查找
        </Button>
        <p style={{ color: 'var(--yx-text-secondary)', fontSize: 12 }}>
          按画面查找会使用视觉模型，每次最多查看 4 张，结果是待人工确认的候选。
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
            style={{ borderBottom: '1px solid var(--yx-border)', paddingBottom: 16 }}
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
        {visual && <>
          <p>已检查 {visual.checked} / {visual.total} 张；模型判断可能有误，请查看图片核实。</p>
          {[...visual.candidates].sort((a, b) =>
            ({ MATCH: 0, UNCERTAIN: 1, NO_MATCH: 2 }[a.match] - { MATCH: 0, UNCERTAIN: 1, NO_MATCH: 2 }[b.match]))
            .map(({ image, match, observation }) => (
              <div key={`${image.bundle_sha}:${image.asset_id}`} style={{ borderBottom: '1px solid var(--yx-border)', paddingBottom: 16 }}>
                <p>{match === 'MATCH' ? '可能匹配' : match === 'NO_MATCH' ? '可能不匹配' : '待确认'}：{observation}</p>
                <WikiSourcePicture image={image} onOpenSource={(item) => openDocument({
                  docId: item.document_id, fileName: item.source_name, mediaType: inferMediaType(item.source_name),
                })} />
                <ReadPicture kbId={kbId} image={image} />
              </div>
            ))}
          {visual.hasMore && <Button onClick={() => visualSearch(true)} disabled={loading}>继续查看下一批画面</Button>}
        </>}
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
          <small style={{ color: 'var(--yx-text-secondary)' }}>
            已读取当前图片{result.cached ? '（复用之前的同图同问题记录）' : ''} · {result.read_id}
          </small>
        </div>
      )}
    </>
  );
}
