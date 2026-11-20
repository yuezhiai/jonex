/**
 * 「添加业务节点」浮层。
 *
 * 菜单内容直接取自后端内置步骤清单（`GET /task-center/steps`）：能加什么、每步吃哪些
 * 变量，都以清单为准，前端不再自己维护一份「可选能力」表 —— 两份表迟早对不上。
 * 不可再选的节点卡置灰，原因由页面传入，保证「看着能点」与真正插入校验是同一套逻辑。
 */
import type { ReactNode } from 'react';
import { useState } from 'react';
import { Popover, theme, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import type { TaskCatalogStep } from '../../api/taskCenterDefinitions';
import WorkflowNodeIcon from './WorkflowNodeIcon';

interface AddStepPopoverProps {
  children: ReactNode;
  /** 后端内置步骤清单。 */
  steps: TaskCatalogStep[];
  onSelect: (stepCode: string) => void;
  /** 该步骤为什么不能加：null 才能选；页面用同一个函数做真正的插入校验 */
  blockedReason: (stepCode: string) => 'added' | 'needParse' | null;
  placement?: 'bottom' | 'right';
}

/** 步骤名的本地化：清单里存的是中文名，已知的 step_code 用本地文案覆盖。 */
function stepLabel(t: (key: string) => string, step: TaskCatalogStep): string {
  const key = `taskCenter.editor.node.${step.step_code}`;
  const label = t(key);
  return label === key ? step.name : label;
}

export default function AddStepPopover({
  children,
  steps,
  onSelect,
  blockedReason,
  placement = 'right',
}: AddStepPopoverProps) {
  const { t } = useTranslation();
  const { token } = theme.useToken();
  const [open, setOpen] = useState(false);

  // 解析归「文档处理」，其余归「知识处理」：分组只是菜单的可读性，不参与校验
  const groups = [
    { key: 'taskCenter.editor.group.document', items: steps.filter((step) => step.step_code === 'parse') },
    { key: 'taskCenter.editor.group.knowledge', items: steps.filter((step) => step.step_code !== 'parse') },
  ].filter((group) => group.items.length > 0);

  const content = (
    <div style={{ width: 420, padding: '4px 0' }}>
      {groups.map((group, groupIndex) => (
        <div
          key={group.key}
          style={{
            paddingTop: groupIndex === 0 ? 0 : 16,
            marginTop: groupIndex === 0 ? 0 : 12,
            borderTop: groupIndex === 0 ? undefined : `1px solid ${token.colorSplit}`,
          }}
        >
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t(group.key)}
          </Typography.Text>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
              gap: 8,
              marginTop: 10,
            }}
          >
            {group.items.map((step) => {
              const reason = blockedReason(step.step_code);
              const disabled = reason !== null;
              const select = (): void => {
                if (disabled) return;
                onSelect(step.step_code);
                setOpen(false);
              };
              const descriptionKey = `taskCenter.editor.nodeDesc.${step.step_code}`;
              const description = t(descriptionKey);
              return (
                <div
                  key={step.step_code}
                  role="button"
                  tabIndex={disabled ? -1 : 0}
                  aria-disabled={disabled}
                  onClick={select}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      select();
                    }
                  }}
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 10,
                    padding: '10px 12px',
                    border: `1px solid ${token.colorBorderSecondary}`,
                    borderRadius: token.borderRadiusLG,
                    cursor: disabled ? 'not-allowed' : 'pointer',
                    background: disabled ? token.colorFillQuaternary : token.colorBgContainer,
                    opacity: disabled ? 0.55 : 1,
                  }}
                  className="yx-add-step-option"
                >
                  <WorkflowNodeIcon kind={step.step_code} size={28} />
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 500, lineHeight: '20px' }}>{stepLabel(t, step)}</div>
                    <Typography.Text type="secondary" style={{ fontSize: 12, lineHeight: '18px' }}>
                      {reason === 'added'
                        ? t('taskCenter.editor.step.added')
                        : reason === 'needParse'
                          ? t('taskCenter.editor.step.needParse')
                          : description === descriptionKey
                            ? step.step_code
                            : description}
                    </Typography.Text>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      placement={placement}
      arrow={false}
      content={content}
      overlayStyle={{ maxWidth: 460 }}
      title={t('taskCenter.editor.action.addStep')}
    >
      {children}
    </Popover>
  );
}
