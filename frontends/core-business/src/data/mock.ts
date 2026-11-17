export interface DomainSpace {
  id: string;
  name: string;
  description: string;
  status: 'active' | 'inactive';
  docCount: number;
  knowledgeCount: number;
  createdAt: string;
  icon?: string;
}

export interface KnowledgeItem {
  id: string;
  title: string;
  domain: string;
  sourceType: string;
  status: 'published' | 'draft' | 'processing' | 'error';
  author: string;
  updatedAt: string;
  tags: string[];
  viewCount: number;
}

export interface Domain {
  id: string;
  name: string;
  code: string;
  description: string;
  serviceCount: number;
  knowledgeCount: number;
  searchEnabled: boolean;
  status: 'active' | 'inactive';
  createdAt: string;
}

export interface KnowledgeSearchResult {
  id: string;
  title: string;
  domain: string;
  snippet: string;
  source: string;
  relevance: number;
  updatedAt: string;
}

export const MOCK_KNOWLEDGE_ITEMS: KnowledgeItem[] = [
  {
    id: '1',
    title: 'Cardiovascular Disease Diagnosis Expert Consensus 2025',
    domain: 'Healthcare',
    sourceType: 'PDF',
    status: 'published',
    author: 'Alice Zhang',
    updatedAt: '2026-05-15',
    tags: ['Cardiovascular', 'Diagnosis', 'Consensus'],
    viewCount: 2345,
  },
  {
    id: '2',
    title: 'Financial Risk Model Data Preprocessing Standards',
    domain: 'FinTech',
    sourceType: 'Word Document',
    status: 'published',
    author: 'Bob Li',
    updatedAt: '2026-05-14',
    tags: ['Risk', 'Preprocessing'],
    viewCount: 1876,
  },
  {
    id: '3',
    title: 'Smart Manufacturing Fault Diagnosis Algorithm Comparison',
    domain: 'Manufacturing',
    sourceType: 'PDF',
    status: 'processing',
    author: 'Carol Wang',
    updatedAt: '2026-05-13',
    tags: ['Fault Diagnosis', 'Algorithm'],
    viewCount: 956,
  },
  {
    id: '4',
    title: 'Personalized Learning Path Recommendation with Knowledge Graphs',
    domain: 'Education',
    sourceType: 'API',
    status: 'published',
    author: 'Daisy Chen',
    updatedAt: '2026-05-12',
    tags: ['Knowledge Graph', 'Recommendation'],
    viewCount: 3210,
  },
  {
    id: '5',
    title: 'Retail Customer Behavior Analysis & RFM Model Application',
    domain: 'E-commerce',
    sourceType: 'CSV',
    status: 'draft',
    author: 'Eve Zhao',
    updatedAt: '2026-05-11',
    tags: ['Behavior Analysis', 'RFM'],
    viewCount: 654,
  },
  {
    id: '6',
    title: 'Carbon Emission Accounting Methodology Guide',
    domain: 'Energy',
    sourceType: 'PDF',
    status: 'error',
    author: 'Frank Liu',
    updatedAt: '2026-05-10',
    tags: ['Carbon', 'Methodology'],
    viewCount: 432,
  },
  {
    id: '7',
    title: 'Deep Learning in Drug Discovery: A Review',
    domain: 'Healthcare',
    sourceType: 'PDF',
    status: 'published',
    author: 'Grace Zhou',
    updatedAt: '2026-05-09',
    tags: ['Deep Learning', 'Drug Discovery'],
    viewCount: 1876,
  },
  {
    id: '8',
    title: 'RegTech in Anti-Money Laundering Practices',
    domain: 'FinTech',
    sourceType: 'API',
    status: 'published',
    author: 'Henry Wu',
    updatedAt: '2026-05-08',
    tags: ['RegTech', 'AML'],
    viewCount: 1456,
  },
];

export const MOCK_DOMAINS: Domain[] = [
  {
    id: '1',
    name: 'Healthcare',
    code: 'medical',
    description: 'Clinical medicine, drug R&D, public health',
    serviceCount: 12,
    knowledgeCount: 3456,
    searchEnabled: true,
    status: 'active',
    createdAt: '2025-06-01',
  },
  {
    id: '2',
    name: 'FinTech',
    code: 'finance',
    description: 'Risk models, quantitative trading, compliance',
    serviceCount: 8,
    knowledgeCount: 2340,
    searchEnabled: true,
    status: 'active',
    createdAt: '2025-06-15',
  },
  {
    id: '3',
    name: 'Manufacturing',
    code: 'manufacture',
    description: 'Industry 4.0, predictive maintenance, digital twins',
    serviceCount: 6,
    knowledgeCount: 1823,
    searchEnabled: true,
    status: 'active',
    createdAt: '2025-07-01',
  },
  {
    id: '4',
    name: 'Education',
    code: 'education',
    description: 'Adaptive learning, resource recommendation, analytics',
    serviceCount: 10,
    knowledgeCount: 2891,
    searchEnabled: true,
    status: 'active',
    createdAt: '2025-07-15',
  },
  {
    id: '5',
    name: 'E-commerce',
    code: 'retail',
    description: 'User profiling, recommendations, supply chain',
    serviceCount: 5,
    knowledgeCount: 1654,
    searchEnabled: false,
    status: 'active',
    createdAt: '2025-08-01',
  },
  {
    id: '6',
    name: 'Energy',
    code: 'energy',
    description: 'Carbon management, renewables, environmental monitoring',
    serviceCount: 4,
    knowledgeCount: 678,
    searchEnabled: false,
    status: 'inactive',
    createdAt: '2025-08-15',
  },
];

export interface GraphInstance {
  id: string;
  name: string;
  type: string;
  properties: { key: string; value: string }[];
  relations: { id: string; name: string; target: string }[];
  sourceDocs: string[];
}

export interface GraphRelation {
  id: string;
  name: string;
  sourceEntity: string;
  targetEntity: string;
  type: string;
  properties: { key: string; value: string }[];
  sourceSnippets: string[];
}

export const MOCK_GRAPH_INSTANCES: GraphInstance[] = [
  {
    id: 'inst-001',
    name: 'Enterprise Digital Transformation',
    type: 'Concept',
    properties: [
      { key: 'Alias', value: 'Digital Transformation, Digital Change' },
      {
        key: 'Definition',
        value:
          'Systematic process of reshaping business operations, organizational structure, and culture through digital technology',
      },
      { key: 'Source', value: 'Enterprise Digital Transformation White Paper' },
      { key: 'Confidence', value: '0.95' },
    ],
    relations: [
      { id: 'rel-001', name: 'Includes', target: 'Cloud Computing' },
      { id: 'rel-002', name: 'Depends On', target: 'Data Governance' },
      { id: 'rel-003', name: 'Drives', target: 'Business Innovation' },
    ],
    sourceDocs: ['digital_transformation_whitepaper.pdf', 'transformation_case_study.docx'],
  },
  {
    id: 'inst-002',
    name: 'Knowledge Graph',
    type: 'Technology',
    properties: [
      { key: 'Also Known As', value: 'Knowledge Graph' },
      { key: 'Proposed By', value: 'Google, 2012' },
      { key: 'Applications', value: 'Semantic Search, Q&A, Recommendation Systems' },
    ],
    relations: [
      { id: 'rel-004', name: 'Belongs To', target: 'AI Technology' },
      { id: 'rel-005', name: 'Uses', target: 'Graph Database' },
    ],
    sourceDocs: ['knowledge_graph_survey.pdf'],
  },
];

export const MOCK_GRAPH_RELATIONS: GraphRelation[] = [
  {
    id: 'rel-001',
    name: 'Includes',
    sourceEntity: 'Enterprise Digital Transformation',
    targetEntity: 'Cloud Computing',
    type: 'Composition',
    properties: [
      { key: 'Weight', value: '0.85' },
      { key: 'Source', value: 'Auto Extraction' },
    ],
    sourceSnippets: [
      'Digital transformation includes cloud computing, big data, AI...',
      'Cloud computing serves as the foundational platform...',
    ],
  },
  {
    id: 'rel-002',
    name: 'Depends On',
    sourceEntity: 'Enterprise Digital Transformation',
    targetEntity: 'Data Governance',
    type: 'Dependency',
    properties: [
      { key: 'Weight', value: '0.78' },
      { key: 'Source', value: 'Expert Annotation' },
    ],
    sourceSnippets: ['Digital transformation depends on a robust data governance framework...'],
  },
];
