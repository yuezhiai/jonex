export interface Adapter {
  id: string;
  name: string;
  type: string;
  protocol: string;
  targetSystem: string;
  status: 'active' | 'inactive' | 'error';
  throughput: string;
  lastHeartbeat: string;
}
export interface MarketplaceItem {
  id: string;
  name: string;
  category: string;
  vendor: string;
  description: string;
  status: 'available' | 'installed' | 'coming';
  rating: number;
  users: number;
}
export interface Scenario {
  id: string;
  name: string;
  industry: string;
  description: string;
  useCases: string[];
  maturity: 'production' | 'pilot' | 'planning';
  adapterCount: number;
}

export const MOCK_ADAPTERS: Adapter[] = [
  {
    id: '1',
    name: 'TCADP适配器',
    type: '平台对接',
    protocol: 'HTTPS/REST',
    targetSystem: '腾讯TCADP平台',
    status: 'active',
    throughput: '1200 req/min',
    lastHeartbeat: '2026-05-23 09:45:00',
  },
  {
    id: '2',
    name: '企业微信适配器',
    type: '消息通道',
    protocol: 'Webhook',
    targetSystem: '企业微信',
    status: 'active',
    throughput: '560 msg/min',
    lastHeartbeat: '2026-05-23 09:44:30',
  },
  {
    id: '3',
    name: '钉钉适配器',
    type: '消息通道',
    protocol: 'Webhook',
    targetSystem: '钉钉开放平台',
    status: 'inactive',
    throughput: '0 msg/min',
    lastHeartbeat: '2026-05-15 12:00:00',
  },
  {
    id: '4',
    name: '飞书适配器',
    type: '消息通道',
    protocol: 'Webhook',
    targetSystem: '飞书开放平台',
    status: 'active',
    throughput: '320 msg/min',
    lastHeartbeat: '2026-05-23 09:43:15',
  },
  {
    id: '5',
    name: '微信公众号适配器',
    type: '用户触达',
    protocol: 'OAuth2.0',
    targetSystem: '微信公众号',
    status: 'error',
    throughput: '0',
    lastHeartbeat: '2026-05-22 18:00:00',
  },
  {
    id: '6',
    name: 'Slack适配器',
    type: '消息通道',
    protocol: 'Webhook',
    targetSystem: 'Slack API',
    status: 'inactive',
    throughput: '0',
    lastHeartbeat: '2026-04-01 00:00:00',
  },
];

export const MOCK_MARKETPLACE_ITEMS: MarketplaceItem[] = [
  {
    id: '1',
    name: '智能客服助手',
    category: '对话AI',
    vendor: 'MockVendor',
    description: '基于知识库的智能客服，支持多渠道接入',
    status: 'available',
    rating: 4.8,
    users: 2340,
  },
  {
    id: '2',
    name: '文档智能分析',
    category: '文档处理',
    vendor: 'MockVendor',
    description: '多格式文档解析、知识提取、自动分类',
    status: 'installed',
    rating: 4.6,
    users: 1560,
  },
  {
    id: '3',
    name: '代码知识助手',
    category: '开发工具',
    vendor: '合作伙伴',
    description: '面向开发者的技术文档检索问答',
    status: 'available',
    rating: 4.5,
    users: 890,
  },
  {
    id: '4',
    name: '医学文献助手',
    category: '行业应用',
    vendor: '合作伙伴',
    description: '医学文献自动检索、摘要生成',
    status: 'installed',
    rating: 4.9,
    users: 420,
  },
  {
    id: '5',
    name: '法律文书审查',
    category: '行业应用',
    vendor: '合作伙伴',
    description: '合同审查、法规检索、判例分析',
    status: 'coming',
    rating: 0,
    users: 0,
  },
  {
    id: '6',
    name: '工业知识图谱',
    category: '行业应用',
    vendor: 'MockVendor',
    description: '设备故障知识图谱构建与诊断推理',
    status: 'available',
    rating: 4.3,
    users: 670,
  },
];

export const MOCK_SCENARIOS: Scenario[] = [
  {
    id: '1',
    name: '智慧医疗临床决策支持',
    industry: '医疗健康',
    description: '整合临床指南、药物知识库、病例数据，提供诊疗决策辅助',
    useCases: ['临床知识检索', '药物相互作用检查', '相似病例分析'],
    maturity: 'production',
    adapterCount: 4,
  },
  {
    id: '2',
    name: '金融智能风控',
    industry: '金融科技',
    description: '基于知识图谱的风险识别、反欺诈和合规检查',
    useCases: ['反洗钱筛查', '信用评估', '监管合规'],
    maturity: 'pilot',
    adapterCount: 3,
  },
  {
    id: '3',
    name: '智能制造预测性维护',
    industry: '智能制造',
    description: '设备故障知识库 + 实时传感器数据分析',
    useCases: ['故障诊断', '维修建议', '备件推荐'],
    maturity: 'production',
    adapterCount: 5,
  },
  {
    id: '4',
    name: '个性化自适应学习',
    industry: '教育培训',
    description: '基于知识图谱的学科知识管理与学习路径推荐',
    useCases: ['知识点导航', '学习路径规划', '薄弱点诊断'],
    maturity: 'pilot',
    adapterCount: 2,
  },
  {
    id: '5',
    name: '零售智能推荐与客服',
    industry: '零售电商',
    description: '用户画像分析、个性化推荐和智能客服',
    useCases: ['商品推荐', '智能客服', '评论分析'],
    maturity: 'planning',
    adapterCount: 1,
  },
];

// Skills
export interface Skill {
  id: string;
  name: string;
  category: string;
  description: string;
  status: 'enabled' | 'disabled' | 'draft';
  version: string;
  updatedAt: string;
}

export const MOCK_SKILLS: Skill[] = [
  {
    id: 'sk-1',
    name: '文本摘要',
    category: 'NLP',
    description: '自动生成文档摘要和关键信息提取',
    status: 'enabled',
    version: 'v1.2.0',
    updatedAt: '2026-06-01',
  },
  {
    id: 'sk-2',
    name: '情感分析',
    category: 'NLP',
    description: '分析文本情感倾向和情绪识别',
    status: 'enabled',
    version: 'v2.0.1',
    updatedAt: '2026-05-28',
  },
  {
    id: 'sk-3',
    name: '实体抽取',
    category: '知识图谱',
    description: '从非结构化文本中抽取命名实体',
    status: 'enabled',
    version: 'v1.5.0',
    updatedAt: '2026-06-02',
  },
  {
    id: 'sk-4',
    name: '图像识别',
    category: '计算机视觉',
    description: '识别图像中的物体、场景和文字',
    status: 'draft',
    version: 'v0.9.0',
    updatedAt: '2026-05-20',
  },
  {
    id: 'sk-5',
    name: '语音转写',
    category: '语音',
    description: '将音频文件转写为结构化文本',
    status: 'disabled',
    version: 'v1.0.0',
    updatedAt: '2026-04-15',
  },
  {
    id: 'sk-6',
    name: '知识问答',
    category: '知识图谱',
    description: '基于知识库的智能问答能力',
    status: 'enabled',
    version: 'v2.1.0',
    updatedAt: '2026-06-01',
  },
];

// Skills 卡片原型 mock 数据（与原型页面一致）
export interface SkillCardItem {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  skill_type: string;
  config_json: Record<string, unknown>;
  status: string;
  model_ids: string[];
  framework_ids: string[];
  created_at: string | null;
  updated_at: string | null;
}

export const MOCK_SKILL_CARDS: SkillCardItem[] = [
  {
    id: 'mock-1',
    tenant_id: 'tenant_jonex_demo',
    name: '图像识别与分析',
    description: '对图片内容进行智能识别，提取物体、文字、场景等多模态信息，支持OCR文字识别',
    skill_type: 'image',
    config_json: { tags: ['图像', 'OCR', '识别'] },
    status: 'enabled',
    model_ids: [],
    framework_ids: [],
    created_at: '2026-05-20T10:30:00',
    updated_at: '2026-06-01T09:00:00',
  },
  {
    id: 'mock-2',
    tenant_id: 'tenant_jonex_demo',
    name: '语音转文本',
    description: '将音频文件中的语音内容自动转录为结构化文本，支持多语种和说话人分离',
    skill_type: 'voice',
    config_json: { tags: ['语音', '转录', 'ASR'] },
    status: 'enabled',
    model_ids: [],
    framework_ids: [],
    created_at: '2026-05-18T14:20:00',
    updated_at: '2026-06-02T11:00:00',
  },
  {
    id: 'mock-3',
    tenant_id: 'tenant_jonex_demo',
    name: '文档版面分析',
    description: '分析PDF、图片等文档的版面结构，识别段落、表格、图表、页眉页脚等元素',
    skill_type: 'document',
    config_json: { tags: ['文档', '版面', '结构化'] },
    status: 'enabled',
    model_ids: [],
    framework_ids: [],
    created_at: '2026-05-22T08:00:00',
    updated_at: '2026-05-30T16:30:00',
  },
  {
    id: 'mock-4',
    tenant_id: 'tenant_jonex_demo',
    name: '视频内容理解',
    description: '对视频内容进行抽帧分析，识别场景、动作、人物及事件时间线',
    skill_type: 'video',
    config_json: { tags: ['视频', '抽帧', '分析'] },
    status: 'disabled',
    model_ids: [],
    framework_ids: [],
    created_at: '2026-05-15T12:00:00',
    updated_at: '2026-05-15T12:00:00',
  },
  {
    id: 'mock-5',
    tenant_id: 'tenant_jonex_demo',
    name: '多模态融合检索',
    description: '跨文本、图像、语音等多模态数据的统一语义检索与相似度匹配',
    skill_type: 'fusion',
    config_json: { tags: ['检索', '融合', '语义'] },
    status: 'disabled',
    model_ids: [],
    framework_ids: [],
    created_at: '2026-05-10T09:00:00',
    updated_at: '2026-05-10T09:00:00',
  },
  {
    id: 'mock-6',
    tenant_id: 'tenant_jonex_demo',
    name: '智能数据提取',
    description: '从非结构化文档中自动提取关键字段和结构化数据，支持表格、表单、发票等场景',
    skill_type: 'custom',
    config_json: { tags: ['提取', '结构化', '文档'] },
    status: 'draft',
    model_ids: [],
    framework_ids: [],
    created_at: '2026-05-08T15:00:00',
    updated_at: '2026-05-25T10:00:00',
  },
];

// Template Domains
export interface TemplateDomain {
  id: string;
  name: string;
  status: 'active' | 'inactive';
  createdAt: string;
}

export const MOCK_TEMPLATE_DOMAINS: TemplateDomain[] = [
  { id: 'td-1', name: '金融行业', status: 'active', createdAt: '2026-05-20 10:30' },
  { id: 'td-2', name: '医疗健康', status: 'active', createdAt: '2026-05-18 14:20' },
  { id: 'td-3', name: '制造业', status: 'inactive', createdAt: '2026-05-15 09:10' },
];

// Template Scenarios
export interface TemplateScenario {
  id: string;
  domainId: string;
  domainName: string;
  name: string;
  description: string;
  objectCount: number;
  relationCount: number;
  status: 'published' | 'draft';
}

export const MOCK_TEMPLATE_SCENARIOS: TemplateScenario[] = [
  {
    id: 'ts-1',
    domainId: 'td-1',
    domainName: '医疗健康领域模板',
    name: '临床诊断场景',
    description: '面向临床诊断的知识图谱应用场景',
    objectCount: 45,
    relationCount: 120,
    status: 'published',
  },
  {
    id: 'ts-2',
    domainId: 'td-1',
    domainName: '医疗健康领域模板',
    name: '药物研发场景',
    description: '新药研发知识管理与关联分析',
    objectCount: 38,
    relationCount: 95,
    status: 'published',
  },
  {
    id: 'ts-3',
    domainId: 'td-2',
    domainName: '金融风控领域模板',
    name: '企业信用评估',
    description: '企业信用风险评估与关联分析',
    objectCount: 52,
    relationCount: 140,
    status: 'published',
  },
  {
    id: 'ts-4',
    domainId: 'td-3',
    domainName: '智能制造领域模板',
    name: '设备预测维护',
    description: '设备故障预测与维护决策支持',
    objectCount: 30,
    relationCount: 78,
    status: 'draft',
  },
];

// Template Objects
export interface TemplateObject {
  id: string;
  name: string;
  domainId: string;
  domainName: string;
  fields: { name: string; type: string; required: boolean }[];
  status: 'active' | 'draft';
}

export const MOCK_TEMPLATE_OBJECTS: TemplateObject[] = [
  {
    id: 'to-1',
    name: '疾病',
    domainId: 'td-1',
    domainName: '医疗健康',
    fields: [
      { name: '疾病名称', type: 'string', required: true },
      { name: 'ICD编码', type: 'string', required: true },
      { name: '症状描述', type: 'text', required: false },
    ],
    status: 'active',
  },
  {
    id: 'to-2',
    name: '药品',
    domainId: 'td-1',
    domainName: '医疗健康',
    fields: [
      { name: '药品名称', type: 'string', required: true },
      { name: '批准文号', type: 'string', required: true },
      { name: '生产企业', type: 'string', required: false },
    ],
    status: 'active',
  },
  {
    id: 'to-3',
    name: '企业',
    domainId: 'td-2',
    domainName: '金融科技',
    fields: [
      { name: '企业名称', type: 'string', required: true },
      { name: '统一社会信用代码', type: 'string', required: true },
      { name: '行业分类', type: 'string', required: false },
    ],
    status: 'active',
  },
];

// Template Relations
export interface TemplateRelation {
  id: string;
  name: string;
  sourceObject: string;
  targetObject: string;
  relationType: string;
  constraints: string;
}

export const MOCK_TEMPLATE_RELATIONS: TemplateRelation[] = [
  {
    id: 'tr-1',
    name: '治疗',
    sourceObject: '药品',
    targetObject: '疾病',
    relationType: '1:N',
    constraints: '药品可治疗多种疾病',
  },
  {
    id: 'tr-2',
    name: '包含成分',
    sourceObject: '药品',
    targetObject: '化合物',
    relationType: 'N:M',
    constraints: '药品可包含多种化合物',
  },
  {
    id: 'tr-3',
    name: '控股',
    sourceObject: '企业',
    targetObject: '企业',
    relationType: '1:N',
    constraints: '母子公司关系',
  },
];
