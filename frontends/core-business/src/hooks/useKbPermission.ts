import { useEffect, useState } from 'react';
import { getDomainKnowledgeDetail } from '@/api/domainKnowledge';

/**
 * KB 权限标志 hook（权限矩阵 2026-08-20）：
 * - canWrite：KB 内容写（空间 owner/manager/租户管理员 或 授权 editor）——
 *   上传文档/数据源 CRUD/编译/本体 schema/同义词/解析器设置等按钮依据
 * - canManage：KB 管理（空间 owner/manager/租户管理员，不含 editor）——
 *   删除 KB/权限管理按钮依据
 *
 * 详情页（DomainKnowledgeDetail）组件树内直接使用 detail.can_write /
 * detail.can_manage_permissions，不走本 hook；本 hook 供列表外页面
 * （文档结果/编译结果/数据源系列等）按 kbId 拉取。
 */
export function useKbPermission(kbId?: string) {
  const [canWrite, setCanWrite] = useState(false);
  const [canManage, setCanManage] = useState(false);

  useEffect(() => {
    if (!kbId) {
      setCanWrite(false);
      setCanManage(false);
      return;
    }
    let alive = true;
    getDomainKnowledgeDetail(kbId)
      .then((d) => {
        if (!alive) return;
        setCanWrite(d.can_write === true);
        setCanManage(d.can_manage_permissions === true);
      })
      .catch(() => {
        if (!alive) return;
        setCanWrite(false);
        setCanManage(false);
      });
    return () => {
      alive = false;
    };
  }, [kbId]);

  return { canWrite, canManage };
}
