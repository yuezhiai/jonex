import {
  HomeOutlined,
  SettingOutlined,
  GlobalOutlined,
  SearchOutlined,
  DatabaseOutlined,
  ClusterOutlined,
  BlockOutlined,
  FileTextOutlined,
  ApiOutlined,
  ThunderboltOutlined,
  CopyOutlined,
} from '@ant-design/icons';
import type { NavSection } from './navTypes';

export const prototypeNavConfig: NavSection[] = [
  {
    key: 'core-business',
    label: 'navigation.coreBusiness',
    icon: HomeOutlined,
    items: [
      {
        key: 'knowledge-search',
        label: 'navigation.knowledgeSearch',
        appId: 'core-business',
        internalPath: 'knowledge-search',
        icon: SearchOutlined,
      },
      {
        key: 'domain-knowledge',
        label: 'navigation.domainKnowledge',
        appId: 'core-business',
        internalPath: 'domain-knowledge',
        icon: DatabaseOutlined,
      },
      {
        key: 'domain-management',
        label: 'navigation.domainManagement',
        appId: 'core-business',
        internalPath: 'domain-management',
        icon: ClusterOutlined,
      },
    ],
  },
  {
    key: 'platform-management',
    label: 'navigation.platformManagement',
    icon: SettingOutlined,
    items: [
      {
        key: 'engine-mgmt-group',
        label: 'navigation.engineManagement',
        icon: ApiOutlined,
        children: [
          {
            key: 'data-access',
            label: 'navigation.dataAccess',
            appId: 'platform-management',
            internalPath: 'data-access',
          },
          {
            key: 'parser-management',
            label: 'navigation.parserManagement',
            appId: 'platform-management',
            internalPath: 'parser-management',
          },
          {
            key: 'model-adapter',
            label: 'navigation.modelAdapter',
            appId: 'platform-management',
            internalPath: 'model-adapter',
            hidden: true,
          },
        ],
      },
      {
        key: 'prompt-templates',
        label: 'navigation.promptTemplates',
        appId: 'ecosystem-management',
        internalPath: 'prompt-templates',
        icon: FileTextOutlined,
      },
      {
        key: 'platform-mgmt-group',
        label: 'navigation.administration',
        icon: SettingOutlined,
        children: [
          {
            key: 'tenant-management',
            label: 'navigation.tenantManagement',
            appId: 'platform-management',
            internalPath: 'tenant-management',
            adminOnly: true,
          },
          {
            key: 'user-management',
            label: 'navigation.userManagement',
            appId: 'platform-management',
            internalPath: 'user-management',
          },
          {
            key: 'role-permission',
            label: 'navigation.rolePermission',
            appId: 'platform-management',
            internalPath: 'role-permission',
          },
          {
            key: 'system-config',
            label: 'navigation.systemConfig',
            appId: 'platform-management',
            internalPath: 'system-config',
          },
          {
            key: 'operation-log',
            label: 'navigation.operationLog',
            appId: 'platform-management',
            internalPath: 'operation-log',
          },
        ],
      },
    ],
  },
  {
    key: 'ecosystem-management',
    label: 'navigation.ecosystemManagement',
    icon: GlobalOutlined,
    items: [
      {
        key: 'eco-adapter-group',
        label: 'navigation.ecoAdapter',
        icon: BlockOutlined,
        hidden: false,
        children: [
          {
            key: 'adapter-management',
            label: 'navigation.adapterList',
            appId: 'ecosystem-management',
            internalPath: 'adapter-management',
            hidden: false,
          },
          {
            key: 'mcp-service-directory',
            label: 'navigation.mcpServiceDirectory',
            appId: 'ecosystem-management',
            internalPath: 'mcp-service-directory',
            icon: ClusterOutlined,
          },
        ],
      },
      {
        key: 'eco-skills-group',
        label: 'navigation.skills',
        icon: ThunderboltOutlined,
        hidden: true,
        children: [
          {
            key: 'skills',
            label: 'navigation.skillManagement',
            appId: 'ecosystem-management',
            internalPath: 'skills',
            hidden: true,
          },
        ],
      },
      {
        key: 'template-domains',
        label: 'navigation.templateDomains',
        appId: 'ecosystem-management',
        internalPath: 'template-domains',
        icon: CopyOutlined,
        matchPaths: [
          { appId: 'ecosystem-management', internalPath: 'template-scenarios' },
          { appId: 'ecosystem-management', internalPath: 'template-objects' },
          { appId: 'ecosystem-management', internalPath: 'template-relations' },
        ],
      },
    ],
  },
];
