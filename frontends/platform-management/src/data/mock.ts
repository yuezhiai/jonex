export interface Tenant {
  id: string;
  name: string;
  code: string;
  plan: string;
  userCount: number;
  storageUsed: string;
  status: 'active' | 'suspended';
  createdAt: string;
}
export interface User {
  id: string;
  username: string;
  displayName: string;
  email: string;
  roles: string[];
  tenantName: string;
  status: 'active' | 'disabled';
  lastLogin: string;
}
export interface Role {
  id: string;
  name: string;
  description: string;
  userCount: number;
  permissions: string[];
  status: 'active' | 'inactive';
}
export interface Task {
  id: string;
  name: string;
  type: string;
  cron: string;
  lastRun: string;
  lastStatus: 'success' | 'failed' | 'running';
  nextRun: string;
  enabled: boolean;
}
export interface SystemConfig {
  id: string;
  key: string;
  value: string;
  description: string;
  category: string;
  updatedAt: string;
}
export interface OperationLog {
  id: string;
  operator: string;
  action: string;
  module: string;
  target: string;
  result: 'success' | 'failure';
  ip: string;
  timestamp: string;
}

export const MOCK_TENANTS: Tenant[] = [
  {
    id: '1',
    name: '默认租户',
    code: 'default',
    plan: '企业版',
    userCount: 128,
    storageUsed: '256 GB',
    status: 'active',
    createdAt: '2025-01-01',
  },
  {
    id: '2',
    name: '医疗事业部',
    code: 'medical',
    plan: '专业版',
    userCount: 56,
    storageUsed: '128 GB',
    status: 'active',
    createdAt: '2025-03-15',
  },
  {
    id: '3',
    name: '金融科技部',
    code: 'finance',
    plan: '专业版',
    userCount: 42,
    storageUsed: '96 GB',
    status: 'active',
    createdAt: '2025-04-20',
  },
  {
    id: '4',
    name: '教育研究组',
    code: 'education',
    plan: '基础版',
    userCount: 18,
    storageUsed: '32 GB',
    status: 'active',
    createdAt: '2025-06-10',
  },
  {
    id: '5',
    name: '制造测试组',
    code: 'mfg-test',
    plan: '试用版',
    userCount: 5,
    storageUsed: '8 GB',
    status: 'suspended',
    createdAt: '2025-08-01',
  },
];

export const MOCK_USERS: User[] = [
  {
    id: '1',
    username: 'admin',
    displayName: '系统管理员',
    email: 'admin@jonex.ai',
    roles: ['系统管理员'],
    tenantName: '默认租户',
    status: 'active',
    lastLogin: '2026-05-23 08:30',
  },
  {
    id: '2',
    username: 'zhangming',
    displayName: '张明远',
    email: 'zhangming@jonex.ai',
    roles: ['editor', 'viewer'],
    tenantName: '医疗事业部',
    status: 'active',
    lastLogin: '2026-05-22 16:45',
  },
  {
    id: '3',
    username: 'lifang',
    displayName: '李芳',
    email: 'lifang@jonex.ai',
    roles: ['editor'],
    tenantName: '金融科技部',
    status: 'active',
    lastLogin: '2026-05-22 14:20',
  },
  {
    id: '4',
    username: 'wangjianguo',
    displayName: '王建国',
    email: 'wangjg@jonex.ai',
    roles: ['viewer'],
    tenantName: '默认租户',
    status: 'active',
    lastLogin: '2026-05-21 10:15',
  },
  {
    id: '5',
    username: 'chensiyu',
    displayName: '陈思雨',
    email: 'chensy@jonex.ai',
    roles: ['editor'],
    tenantName: '教育研究组',
    status: 'active',
    lastLogin: '2026-05-20 09:00',
  },
  {
    id: '6',
    username: 'zhaoyang',
    displayName: '赵阳',
    email: 'zhaoyang@jonex.ai',
    roles: ['viewer'],
    tenantName: '制造测试组',
    status: 'disabled',
    lastLogin: '2026-04-15 11:30',
  },
];

export const MOCK_ROLES: Role[] = [
  {
    id: '1',
    name: '系统管理员',
    description: '平台全权限管理员',
    userCount: 2,
    permissions: ['平台管理', '用户管理', '租户管理', '系统配置', '日志查看', '任务调度', '全部应用'],
    status: 'active',
  },
  {
    id: '2',
    name: '知识编辑者',
    description: '可创建和编辑知识内容',
    userCount: 28,
    permissions: ['知识检索', '知识编辑', '领域管理', '知识发布'],
    status: 'active',
  },
  {
    id: '3',
    name: '数据工程师',
    description: '数据接入和解析管理',
    userCount: 12,
    permissions: ['数据源管理', '解析器管理', '知识编译', '检索引擎'],
    status: 'active',
  },
  {
    id: '4',
    name: '观察者',
    description: '只读权限',
    userCount: 86,
    permissions: ['知识检索', '领域浏览'],
    status: 'active',
  },
];

export const MOCK_TASKS: Task[] = [
  {
    id: '1',
    name: '知识图谱同步',
    type: '数据同步',
    cron: '0 2 * * *',
    lastRun: '2026-05-23 02:00',
    lastStatus: 'success',
    nextRun: '2026-05-24 02:00',
    enabled: true,
  },
  {
    id: '2',
    name: '向量索引重建',
    type: '索引维护',
    cron: '0 3 * * 0',
    lastRun: '2026-05-21 03:00',
    lastStatus: 'success',
    nextRun: '2026-05-28 03:00',
    enabled: true,
  },
  {
    id: '3',
    name: '数据源同步',
    type: '数据同步',
    cron: '*/30 * * * *',
    lastRun: '2026-05-23 09:30',
    lastStatus: 'failed',
    nextRun: '2026-05-23 10:00',
    enabled: true,
  },
  {
    id: '4',
    name: '日志归档清理',
    type: '系统维护',
    cron: '0 4 * * 1',
    lastRun: '2026-05-20 04:00',
    lastStatus: 'success',
    nextRun: '2026-05-27 04:00',
    enabled: false,
  },
];

export const MOCK_SYSTEM_CONFIGS: SystemConfig[] = [
  {
    id: '1',
    key: 'model.default.llm',
    value: 'deepseek-v3',
    description: '默认大语言模型',
    category: '模型配置',
    updatedAt: '2026-05-20',
  },
  {
    id: '2',
    key: 'model.default.embedding',
    value: 'text-embedding-3-large',
    description: '默认嵌入模型',
    category: '模型配置',
    updatedAt: '2026-05-20',
  },
  {
    id: '3',
    key: 'retrieval.max_top_k',
    value: '50',
    description: '检索最大返回数',
    category: '检索设置',
    updatedAt: '2026-05-18',
  },
  {
    id: '4',
    key: 'compile.max_batch_size',
    value: '1000',
    description: '编译最大批处理数',
    category: '编译设置',
    updatedAt: '2026-05-15',
  },
];

export const MOCK_LOGS: OperationLog[] = [
  {
    id: '1',
    operator: 'admin',
    action: '用户登录',
    module: '认证',
    target: '系统',
    result: 'success',
    ip: '192.168.1.100',
    timestamp: '2026-05-23 09:30:15',
  },
  {
    id: '2',
    operator: 'zhangming',
    action: '创建知识',
    module: '知识管理',
    target: '心血管指南2025',
    result: 'success',
    ip: '192.168.1.101',
    timestamp: '2026-05-23 09:28:42',
  },
  {
    id: '3',
    operator: 'lifang',
    action: '修改领域配置',
    module: '领域管理',
    target: '金融科技',
    result: 'success',
    ip: '192.168.1.102',
    timestamp: '2026-05-23 09:25:10',
  },
  {
    id: '4',
    operator: 'system',
    action: '任务执行',
    module: '任务调度',
    target: '数据源同步',
    result: 'failure',
    ip: '127.0.0.1',
    timestamp: '2026-05-23 09:30:00',
  },
  {
    id: '5',
    operator: 'wangjianguo',
    action: '查看文档',
    module: '知识检索',
    target: '设备故障诊断',
    result: 'success',
    ip: '192.168.1.103',
    timestamp: '2026-05-23 09:20:30',
  },
];

// Data Access
export interface DataAccessMethod {
  id: string;
  name: string;
  type: 'file' | 'api' | 'storage' | 'database';
  description: string;
  enabled: boolean;
  configUrl?: string;
}

export const MOCK_DATA_ACCESS_METHODS: DataAccessMethod[] = [
  {
    id: 'da-1',
    name: '本地文件上传',
    type: 'file',
    description: '支持 PDF、Word、TXT、Markdown 等格式文档上传',
    enabled: true,
  },
  { id: 'da-2', name: 'API 数据同步', type: 'api', description: '通过 REST API 定时同步外部系统数据', enabled: true },
  { id: 'da-3', name: '对象存储直连', type: 'storage', description: '对接 S3/OSS/MinIO 等对象存储服务', enabled: true },
  {
    id: 'da-4',
    name: '数据库直连',
    type: 'database',
    description: '直连 MySQL/PostgreSQL 等关系型数据库',
    enabled: false,
  },
];

// Parser
export interface ParserInfo {
  id: string;
  name: string;
  version: string;
  fileTypes: string[];
  status: 'active' | 'inactive' | 'error';
  description: string;
}

export const MOCK_PARSERS: ParserInfo[] = [
  {
    id: 'p-1',
    name: 'MinerU PDF 解析器',
    version: 'v2.1.0',
    fileTypes: ['PDF', 'DOCX', 'PPTX'],
    status: 'active',
    description: '基于深度学习的文档解析引擎，支持复杂版式',
  },
  {
    id: 'p-2',
    name: 'PyMuPDF 解析器',
    version: 'v1.24.0',
    fileTypes: ['PDF', 'XPS', 'EPUB'],
    status: 'active',
    description: '轻量级 PDF 解析，适合简单文档',
  },
  {
    id: 'p-3',
    name: 'Unstructured IO',
    version: 'v0.15.0',
    fileTypes: ['PDF', 'HTML', 'MD', 'TXT'],
    status: 'active',
    description: '通用文档解析框架，支持多种格式',
  },
  {
    id: 'p-4',
    name: 'Whisper 语音解析器',
    version: 'v3.0.0',
    fileTypes: ['MP3', 'WAV', 'M4A'],
    status: 'inactive',
    description: '语音转文本解析引擎，支持多语言',
  },
  {
    id: 'p-5',
    name: 'Tesseract OCR',
    version: 'v5.3.0',
    fileTypes: ['PNG', 'JPG', 'TIFF'],
    status: 'active',
    description: '图像文字识别，支持扫描件解析',
  },
];

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
