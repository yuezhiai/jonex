import React from 'react';
import KnowledgeGraphPanel from '@/components/KnowledgeGraphPanel';

interface GraphTabProps {
  kbId: string;
  docId?: string;
}

const GraphTab: React.FC<GraphTabProps> = ({ kbId, docId }) => {
  return <KnowledgeGraphPanel kbId={kbId} docId={docId} />;
};

export default GraphTab;
