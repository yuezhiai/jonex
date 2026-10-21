/**
 * 知识编译（`pages/KnowledgeCompile*`）这组页面仍是前端原型、未接接口，
 * 这里是唯一还需要 mock 的地方；其余模块的数据都走 `src/api/`。
 */

// Knowledge Compile
export interface CompileTask {
  id: string;
  name: string;
  type: string;
  status: 'running' | 'completed' | 'failed' | 'pending';
  entityCount: number;
  relationCount: number;
  chunkCount: number;
  updatedAt: string;
}

export const MOCK_COMPILE_TASKS: CompileTask[] = [
  {
    id: 'ct-1',
    name: '医疗知识库编译',
    type: '全量编译',
    status: 'completed',
    entityCount: 12580,
    relationCount: 35600,
    chunkCount: 89200,
    updatedAt: '2026-06-01 14:30',
  },
  {
    id: 'ct-2',
    name: '金融知识库增量编译',
    type: '增量编译',
    status: 'running',
    entityCount: 4520,
    relationCount: 12300,
    chunkCount: 28100,
    updatedAt: '2026-06-02 09:15',
  },
  {
    id: 'ct-3',
    name: '法律知识库编译',
    type: '全量编译',
    status: 'failed',
    entityCount: 0,
    relationCount: 0,
    chunkCount: 0,
    updatedAt: '2026-05-30 18:00',
  },
];
