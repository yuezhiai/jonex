/**
 * 属性面板（选中实体/关系/约束后的编辑区）。
 *
 * 移植自 nexonto 的 `PropertiesPanel.tsx`，三处适配：
 *   - UI 组件换成 Jonex 已有的 antd（原版用 nexonto 自己的 `../ui` kit）
 *   - 砍掉 `ExtractionSkillsEditor`（抽取技能声明，属被裁掉的功能）
 *   - 砍掉 `focusId`/`focusField` 深链聚焦（没有对应的入口）
 *
 * 校验文案不另起一套——重名判定与 `domain/checks.ts` 同源（大小写不敏感），
 * 面板上的即时提示与保存前的闸门才不会互相矛盾。
 */

import { useMemo, useRef, useState, type ReactNode } from 'react';
import {
  ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, PlusOutlined,
} from '@ant-design/icons';
import { Button, Dropdown, Input, Popconfirm, Select, message } from 'antd';
import { useTranslation } from 'react-i18next';

import type {
  Attribute, AttributeType, Cardinality, Constraint, DomainCommand, DomainDocument, EntityType,
  RelationType,
} from '../domain/index.js';
import type { Selection } from '../store/editorStore.js';
import { serverConstraintId } from '../adapters.js';
import type { ServerSchema } from '../adapters.js';
import { AttributeSourcesBlock } from './attributeSources.js';
import type { SynonymIndex } from './matchKernel.js';
import { CardinalityField } from './CardinalityField.js';
import { EndpointTypesField } from './EndpointTypesField.js';

const ATTRIBUTE_TYPES: AttributeType[] = ['string', 'text', 'number', 'integer', 'boolean', 'date', 'datetime', 'enum'];
const RANGE_TYPES: AttributeType[] = ['number', 'integer', 'date', 'datetime'];
const CARDINALITIES: Cardinality[] = ['one_to_one', 'one_to_many', 'many_to_many', 'many_to_one', 'custom'];
type RuleType = Constraint['type'];
const RULE_TYPES: RuleType[] = ['required_attribute', 'unique_attribute', 'value_range', 'query_rule', 'relation_cardinality'];
/** 实体段「加规则」下拉的可选档（关系基数额只在关系段） */
const ENTITY_RULE_TYPES: RuleType[] = ['required_attribute', 'unique_attribute', 'value_range', 'query_rule'];

interface Props {
  document: DomainDocument;
  selection: Selection;
  execute: (command: DomainCommand, selection?: Selection) => void;
  addAttribute: (entityId: string, type: AttributeType) => void;
  /** 规则列表点击 → 面板外层的选中通道（编译画布传 store.select，接线见任务 6；抽象编辑器不传） */
  select?: (selection: Selection) => void;
  /** 实体选中时、字段区之后的扩展块（抽象编辑器注入落点面板；编译画布不传） */
  entityExtras?: (entity: EntityType) => ReactNode;
  /** 关系选中时、字段区之后的扩展块（抽象编辑器注入背书只读展示） */
  relationExtras?: (relation: RelationType) => ReactNode;
  /** 全只读（抽象 schema 处于 active 时传入；编译画布不传 = 现状）。
   *  只闸编辑入口——选中、滚动、查看照旧，只读不是「不可用」。 */
  readOnly?: boolean;
  /** 字段集：`'compiled'`（缺省，编译画布）= 现状；`'abstract'`（抽象编辑器）
   *  = ① 关系分支两态都渲染 `sourceTypes` / `targetTypes` / `inverseOf` 三键（保存链路两端均已收，不再按字段集分流）；
   *  ② 属性区渲染「显示名称」输入与属性来源块（同为仅抽象态往返的字段）。
   *  凡是「该上下文存不下」的可编辑字段，一律不渲染（填了也会静默丢，见 I1 收口）。 */
  fieldSet?: 'compiled' | 'abstract';
  /** 抽象编辑器专用：字段落点编辑要拉该源编译 schema 的列清单（编译画布不传）。 */
  kbId?: string;
}

/** 一格的标签 + 内容。原版有 error/hint 通道，这里只保留 label（错误提示走 checks 的顶部横幅）。 */
function Field({ label, children }: { label: string; children: ReactNode }): ReactNode {
  return (
    <div className="form-field">
      <label className="form-field-label">{label}</label>
      {children}
    </div>
  );
}

export function PropertiesPanel({ document, selection, execute, addAttribute, select, entityExtras, relationExtras, readOnly = false, fieldSet = 'compiled', kbId }: Props): ReactNode {
  const { t } = useTranslation();
  const [pendingDelete, setPendingDelete] = useState(false);
  // 编译 schema 缓存（面板级）：落点自动建议按 ds 复用，避免每个属性各拉一遍。
  const suggestSchemaCache = useRef(new Map<string, ServerSchema | null>());
  const suggestSynonymCache = useRef(new Map<string, SynonymIndex>());

  const entity = selection?.kind === 'entity' ? document.model.entities.find((item) => item.id === selection.id) : undefined;
  const relation = selection?.kind === 'relation' ? document.model.relations.find((item) => item.id === selection.id) : undefined;
  const constraint = selection?.kind === 'constraint' ? document.model.constraints.find((item) => item.id === selection.id) : undefined;

  const entityRelations = useMemo(
    () => (entity ? document.model.relations.filter((item) => item.sourceEntityId === entity.id || item.targetEntityId === entity.id) : []),
    [document.model.relations, entity],
  );
  // 多态端点引用该实体的关系（端点没命中，但 sourceTypes/targetTypes 集合里有该实体）：
  // entity.delete 命令会把该实体从这些集合里摘掉（commands.ts 的 drop()），
  // 摘到只剩 1 个时该键在保存时被丢弃——确认弹窗必须让用户看见这些关系会被改动。
  // 端点命中的关系整条删除（已计入 entityRelations），这里去重不重复计。
  const entityTypeRefs = useMemo(
    () => (entity
      ? document.model.relations.filter((item) =>
          item.sourceEntityId !== entity.id
          && item.targetEntityId !== entity.id
          && ((item.sourceTypes ?? []).includes(entity.id) || (item.targetTypes ?? []).includes(entity.id)))
      : []),
    [document.model.relations, entity],
  );
  const entityRules = useMemo(
    () => (entity ? document.model.constraints.filter((item) => item.type !== 'relation_cardinality' && item.entityId === entity.id) : []),
    [document.model.constraints, entity],
  );
  const relationRules = useMemo(
    () => (relation ? document.model.constraints.filter((item) => item.type === 'relation_cardinality' && item.relationId === relation.id) : []),
    [document.model.constraints, relation],
  );

  if (!selection) {
    return (
      <aside className="properties-panel properties-empty">
        <div>
          <h3>{t('selection.noneTitle')}</h3>
          <p>{t('selection.noneBody')}</p>
        </div>
      </aside>
    );
  }

  const updateAttribute = (entityId: string, attribute: Attribute, changes: Partial<Omit<Attribute, 'id'>>): void =>
    execute({ type: 'attribute.update', entityId, attributeId: attribute.id, changes });

  /**
   * 换约束类型 = 删旧建新。难点在于**为新的类型找一个合法目标**：
   *   - `value_range` 只能落在 number/integer/date/datetime 列上
   *   - 同一目标上不能已有同类型约束（服务端的 `checks.constraint.duplicate` 是 mustFix）
   * 找不到就什么都不做——静默产生一条非法约束比不改更糟。
   */
  const changeConstraintType = (current: Constraint, nextType: RuleType): void => {
    if (current.type === nextType) return;
    let next: Constraint | undefined;

    if (nextType === 'relation_cardinality') {
      const target = document.model.relations.find(
        (candidate) => !document.model.constraints.some(
          (item) => item.id !== current.id && item.type === 'relation_cardinality' && item.relationId === candidate.id,
        ),
      );
      if (target) next = { id: current.id, type: nextType, relationId: target.id, min: 0, max: 1, severity: current.severity };
    } else {
      const keepId = current.type === 'relation_cardinality' ? undefined : current.attributeId;
      // 优先保留原来那个属性——换类型时尽量不跳位置
      const candidates = document.model.entities
        .flatMap((candidateEntity) => candidateEntity.attributes
          .filter((attribute) => nextType !== 'value_range' || RANGE_TYPES.includes(attribute.type))
          .map((attribute) => ({ entity: candidateEntity, attribute })))
        .sort((left, right) => Number(right.attribute.id === keepId) - Number(left.attribute.id === keepId));
      const target = candidates.find(({ attribute }) => !document.model.constraints.some(
        (item) => item.id !== current.id && item.type === nextType && item.attributeId === attribute.id,
      ));
      if (target) {
        if (nextType === 'value_range') {
          next = { id: current.id, type: nextType, entityId: target.entity.id, attributeId: target.attribute.id, min: 0, max: 1, severity: current.severity };
        } else if (nextType === 'query_rule') {
          next = { id: current.id, type: nextType, entityId: target.entity.id, attributeId: target.attribute.id, expression: '', severity: current.severity };
        } else if (nextType === 'required_attribute' || nextType === 'unique_attribute') {
          next = { id: current.id, type: nextType, entityId: target.entity.id, attributeId: target.attribute.id, severity: current.severity };
        }
      }
    }

    if (next) execute({ type: 'constraint.update', constraintId: current.id, constraint: next });
  };

  /** 「加规则」（实体段）：先找合法目标（该实体的属性里、同档未占用的），找不到就提示、静默不建。 */
  const addEntityRule = (type: RuleType): void => {
    // Dropdown 的 key 是外部字符串：不在档表内一律不建（否则会掉进末尾 else 静默建 unique_attribute）
    if (!(ENTITY_RULE_TYPES as readonly string[]).includes(type)) return;
    if (!entity || type === 'relation_cardinality') return;
    const target = entity.attributes.find((attribute) =>
      (type !== 'value_range' || RANGE_TYPES.includes(attribute.type))
      && !document.model.constraints.some((item) => item.type === type && item.attributeId === attribute.id));
    if (!target) {
      message.warning(t('rule.noTarget'));
      return;
    }
    const id = serverConstraintId(type, `${entity.name}.${target.name}`);
    let constraint: Constraint;
    if (type === 'value_range') {
      constraint = { id, type: 'value_range', entityId: entity.id, attributeId: target.id, min: 0, max: 1, severity: 'error' };
    } else if (type === 'query_rule') {
      constraint = { id, type: 'query_rule', entityId: entity.id, attributeId: target.id, expression: '', severity: 'error' };
    } else if (type === 'required_attribute') {
      constraint = { id, type: 'required_attribute', entityId: entity.id, attributeId: target.id, severity: 'error' };
    } else {
      constraint = { id, type: 'unique_attribute', entityId: entity.id, attributeId: target.id, severity: 'error' };
    }
    execute({ type: 'constraint.add', constraint }, { kind: 'constraint', id });
  };

  /** 「加规则」（关系段）：同一关系上的基数规则至多一条（再点即提示）。 */
  const addRelationRule = (): void => {
    if (!relation) return;
    if (relationRules.length) {
      message.warning(t('rule.duplicate'));
      return;
    }
    const id = serverConstraintId('relation_cardinality', relation.name);
    execute(
      { type: 'constraint.add', constraint: { id, type: 'relation_cardinality', relationId: relation.id, min: 0, max: 1, severity: 'error' } },
      { kind: 'constraint', id },
    );
  };

  /** 规则列表的目标短标签（调用点：实体段——取目标属性当前名；悬空时回退 id 拆名）。 */
  const ruleTargetLabel = (rule: Constraint): string => {
    if (rule.type !== 'relation_cardinality') {
      const entityNow = document.model.entities.find((item) => item.id === rule.entityId);
      const attributeNow = entityNow?.attributes.find((item) => item.id === rule.attributeId);
      if (attributeNow) return attributeNow.name;
      return rule.attributeId.split('.').pop() ?? rule.attributeId;
    }
    return rule.relationId.replace(/^r:/, '');
  };

  return (
    <aside className="properties-panel">
      {entity ? (
        <div className="properties-content">
          <div className="panel-heading">
            <h2>{t('entity.properties')}</h2>
            <Popconfirm
              open={pendingDelete}
              title={t('entity.deleteTitle')}
              description={t('entity.deleteMessage', { name: entity.name, relations: entityRelations.length, typeRefs: entityTypeRefs.length, rules: entityRules.length })}
              okText={t('common.delete')}
              cancelText={t('common.cancel')}
              okButtonProps={{ danger: true }}
              onConfirm={() => {
                execute({ type: 'entity.delete', entityId: entity.id }, null);
                setPendingDelete(false);
              }}
              onCancel={() => setPendingDelete(false)}
            >
              {/* aria-label 显式声明：antd 图标会把自身 aria-label（"delete"）并入
                  按钮可访问名，盖住后按名查询与读屏都以本地化文案为准。 */}
              <Button danger type="text" icon={<DeleteOutlined />} disabled={readOnly}
                aria-label={t('entity.deleteTitle')} onClick={() => setPendingDelete(true)} />
            </Popconfirm>
          </div>

          <Field label={t('common.name')}>
            <Input
              disabled={readOnly}
              value={entity.name}
              onChange={(event) => {
                const name = event.target.value;
                // 重名就地不落——与 checks.ts 的判据同源（大小写不敏感）
                if (!document.model.entities.some((item) => item.id !== entity.id && item.name.toLowerCase() === name.toLowerCase())) {
                  execute({ type: 'entity.update', entityId: entity.id, changes: { name } });
                }
              }}
            />
          </Field>
          <Field label={t('entity.displayName')}>
            <Input
              disabled={readOnly}
              value={entity.displayName ?? ''}
              onChange={(event) => execute({ type: 'entity.update', entityId: entity.id, changes: { displayName: event.target.value } })}
            />
          </Field>
          <Field label={t('entity.aliases')}>
            <Input
              disabled={readOnly}
              value={entity.aliases.join(', ')}
              onChange={(event) => execute({
                type: 'entity.update', entityId: entity.id,
                changes: { aliases: [...new Set(event.target.value.split(',').map((item) => item.trim()).filter(Boolean))] },
              })}
            />
          </Field>
          <Field label={t('common.description')}>
            <Input.TextArea
              disabled={readOnly}
              rows={3}
              value={entity.description ?? ''}
              onChange={(event) => execute({ type: 'entity.update', entityId: entity.id, changes: { description: event.target.value } })}
            />
          </Field>

          {entityExtras ? entityExtras(entity) : null}

          <div className="subsection-heading">
            <h3>{t('attribute.title')}</h3>
            <Button size="small" icon={<PlusOutlined />} disabled={readOnly} onClick={() => addAttribute(entity.id, 'string')}>{t('attribute.add')}</Button>
          </div>
          <div className="attribute-editor-list">
            {entity.attributes.map((attribute, index) => (
              <div className="attribute-editor" key={attribute.id}>
                <div className="attribute-editor-top">
                  <strong>{attribute.name || t('attribute.name')}</strong>
                  <div>
                    <Button type="text" size="small" icon={<ArrowUpOutlined />} disabled={readOnly || index === 0}
                      onClick={() => execute({ type: 'attribute.reorder', entityId: entity.id, attributeId: attribute.id, direction: 'up' })} />
                    <Button type="text" size="small" icon={<ArrowDownOutlined />} disabled={readOnly || index === entity.attributes.length - 1}
                      onClick={() => execute({ type: 'attribute.reorder', entityId: entity.id, attributeId: attribute.id, direction: 'down' })} />
                    <Button type="text" size="small" danger icon={<DeleteOutlined />} disabled={readOnly}
                      onClick={() => execute({ type: 'attribute.delete', entityId: entity.id, attributeId: attribute.id })} />
                  </div>
                </div>
                <Field label={t('attribute.name')}>
                  <Input
                    disabled={readOnly}
                    value={attribute.name}
                    onChange={(event) => {
                      const name = event.target.value;
                      if (!entity.attributes.some((item) => item.id !== attribute.id && item.name.toLowerCase() === name.toLowerCase())) {
                        updateAttribute(entity.id, attribute, { name });
                      }
                    }}
                  />
                </Field>
                {fieldSet === 'abstract' ? (
                  // 显示名称只在抽象态渲染：v2 保存链路收它（adapters.toServerBody ↔ 后端
                  // `_normalize_attribute`），而编译画布的 adapters 两端都不建模该字段 ——
                  // 渲染成可编辑 = 白填、刷新即静默丢（同关系三格的 I1 口径）。
                  <Field label={t('attribute.displayName')}>
                    <Input
                      disabled={readOnly}
                      value={attribute.displayName ?? ''}
                      onChange={(event) => updateAttribute(entity.id, attribute, { displayName: event.target.value })}
                    />
                  </Field>
                ) : null}
                <Field label={t('attribute.type')}>
                  <Select
                    disabled={readOnly}
                    value={attribute.type}
                    onChange={(type) => updateAttribute(entity.id, attribute, { type })}
                    options={ATTRIBUTE_TYPES.map((type) => ({ value: type, label: t(`types.${type}`) }))}
                  />
                </Field>
                <div className="check-row">
                  <label>
                    <input type="checkbox" checked={attribute.required} disabled={readOnly}
                      onChange={(event) => updateAttribute(entity.id, attribute, { required: event.target.checked })} />
                    {t('common.required')}
                  </label>
                  <label>
                    <input type="checkbox" checked={attribute.primaryKey} disabled={readOnly}
                      onChange={(event) => updateAttribute(entity.id, attribute, {
                        primaryKey: event.target.checked,
                        // 主键必然非空——服务端 checks 把「主键但可空」判为 mustFix
                        ...(event.target.checked ? { required: true } : {}),
                      })} />
                    {t('attribute.primaryKey')}
                  </label>
                </div>
                {attribute.type === 'enum' ? (
                  <Field label={t('attribute.enumValues')}>
                    <Input
                      disabled={readOnly}
                      value={(attribute.enumValues ?? []).join(', ')}
                      onChange={(event) => updateAttribute(entity.id, attribute, {
                        enumValues: event.target.value.split(',').map((item) => item.trim()).filter(Boolean),
                      })}
                    />
                  </Field>
                ) : null}
                <Field label={t('common.description')}>
                  <Input.TextArea rows={2} disabled={readOnly} value={attribute.description ?? ''}
                    onChange={(event) => updateAttribute(entity.id, attribute, { description: event.target.value })} />
                </Field>
                {fieldSet === 'abstract' && kbId ? (
                  <AttributeSourcesBlock kbId={kbId} entity={entity} attribute={attribute}
                    execute={execute} readOnly={readOnly} schemaCache={suggestSchemaCache}
                    synonymCache={suggestSynonymCache} />
                ) : null}
              </div>
            ))}
          </div>

          {fieldSet === 'compiled' ? (
            <>
              <div className="subsection-heading">
                <h3>{t('rule.listTitle')}</h3>
                <Dropdown
                  disabled={readOnly}
                  trigger={['click']}
                  menu={{
                    items: ENTITY_RULE_TYPES.map((ruleType) => ({ key: ruleType, label: t(`rule.${ruleType}`) })),
                    onClick: ({ key }) => addEntityRule(key as RuleType),
                  }}
                >
                  {/* aria-label 显式声明：antd 图标会把自身 aria-label（"plus"）并进
                      按钮可访问名，盖住后读屏只念本地化文案（按名查询也因此稳定）。 */}
                  <Button size="small" icon={<PlusOutlined />} aria-label={t('rule.add')}>{t('rule.add')}</Button>
                </Dropdown>
              </div>
              <div className="rule-list">
                {entityRules.map((rule) => (
                  <button
                    key={rule.id}
                    type="button"
                    className={`rule-list-item ${selection?.kind === 'constraint' && selection.id === rule.id ? 'is-selected' : ''}`}
                    onClick={() => select?.({ kind: 'constraint', id: rule.id })}
                  >
                    <span className="rule-list-type">{t(`rule.${rule.type}`)}</span>
                    <span className="rule-list-target">{ruleTargetLabel(rule)}</span>
                  </button>
                ))}
              </div>
            </>
          ) : null}
        </div>
      ) : relation ? (
        <div className="properties-content">
          <div className="panel-heading">
            <h2>{t('relation.properties')}</h2>
            <Popconfirm
              open={pendingDelete}
              title={t('relation.deleteTitle')}
              description={t('relation.deleteMessage', { name: relation.name, rules: relationRules.length })}
              okText={t('common.delete')}
              cancelText={t('common.cancel')}
              okButtonProps={{ danger: true }}
              onConfirm={() => {
                execute({ type: 'relation.delete', relationId: relation.id }, null);
                setPendingDelete(false);
              }}
              onCancel={() => setPendingDelete(false)}
            >
              <Button danger type="text" icon={<DeleteOutlined />} disabled={readOnly} onClick={() => setPendingDelete(true)} />
            </Popconfirm>
          </div>

          <Field label={t('common.name')}>
            <Input
              disabled={readOnly}
              value={relation.name}
              onChange={(event) => {
                const name = event.target.value;
                if (!document.model.relations.some((item) => item.id !== relation.id && item.name.toLowerCase() === name.toLowerCase())) {
                  execute({ type: 'relation.update', relationId: relation.id, changes: { name } });
                }
              }}
            />
          </Field>
          <Field label={t('relation.source')}>
            <Select
              disabled={readOnly}
              value={relation.sourceEntityId}
              onChange={(sourceEntityId) => execute({ type: 'relation.update', relationId: relation.id, changes: { sourceEntityId } })}
              options={document.model.entities.map((item) => ({ value: item.id, label: item.displayName ?? item.name }))}
            />
          </Field>
          <Field label={t('relation.target')}>
            <Select
              disabled={readOnly}
              value={relation.targetEntityId}
              onChange={(targetEntityId) => execute({ type: 'relation.update', relationId: relation.id, changes: { targetEntityId } })}
              options={document.model.entities.map((item) => ({ value: item.id, label: item.displayName ?? item.name }))}
            />
          </Field>
          {/* 三键两态都渲染：保存链路两端都已收（命令层通用 assign + 抽象适配器
              双向搬三键 + 后端 normalize_schema 一期已收），抽象态的渲染闸是历史遗留。 */}
          {(
            <>
              <Field label={t('relation.sourceTypes')}>
                {/* 这两个子组件（EndpointTypesField / CardinalityField）没有 disabled 通道，
                    且不在本波可改文件里：只读时外层挡住指针 + onChange 收成 no-op（双保险）。 */}
                <div style={readOnly ? { pointerEvents: 'none', opacity: 0.6 } : undefined}>
                  <EndpointTypesField
                    entities={document.model.entities} single={relation.sourceEntityId}
                    value={relation.sourceTypes} field="sourceTypes"
                    onChange={(ids) => { if (!readOnly) execute({ type: 'relation.update', relationId: relation.id, changes: { sourceTypes: ids.length > 1 ? ids : undefined } }); }}
                  />
                </div>
              </Field>
              <Field label={t('relation.targetTypes')}>
                <div style={readOnly ? { pointerEvents: 'none', opacity: 0.6 } : undefined}>
                  <EndpointTypesField
                    entities={document.model.entities} single={relation.targetEntityId}
                    value={relation.targetTypes} field="targetTypes"
                    onChange={(ids) => { if (!readOnly) execute({ type: 'relation.update', relationId: relation.id, changes: { targetTypes: ids.length > 1 ? ids : undefined } }); }}
                  />
                </div>
              </Field>
              {/* 逆关系名：纯声明（服务端 `_build_inverse_map` 拿它给 incoming 边换读法）。
                  机械默认值由生成器给，这里供人工改成业务上说得通的语义名。 */}
              <Field label={t('relation.inverseOf')}>
                <Input
                  className="guard-mono"
                  disabled={readOnly}
                  placeholder="STEP_OF / HAS_MEMBER …"
                  value={relation.inverseOf ?? ''}
                  onChange={(event) => {
                    const value = event.target.value.trim();
                    execute({ type: 'relation.update', relationId: relation.id, changes: { inverseOf: value === '' ? undefined : value } });
                  }}
                />
              </Field>
            </>
          )}
          {/* 多态端点契约提示：source_types/target_types 勾选 >1 才落库，单值等同未声明。
              只抽象态渲染——编译画布必须逐字不变（本线零回归判据）。 */}
          {fieldSet === 'abstract' ? (
            <p className="ae-endpoint-hint">{t('relation.multiEndpointHint')}</p>
          ) : null}
          <Field label={t('relation.cardinality')}>
            <div style={readOnly ? { pointerEvents: 'none', opacity: 0.6 } : undefined}>
              <CardinalityField
                value={relation.cardinality}
                onChange={(next) => { if (!readOnly) execute({ type: 'relation.update', relationId: relation.id, changes: { cardinality: next } }); }}
              />
            </div>
          </Field>
          <Field label={t('common.description')}>
            <Input.TextArea rows={4} disabled={readOnly} value={relation.description ?? ''}
              onChange={(event) => execute({ type: 'relation.update', relationId: relation.id, changes: { description: event.target.value } })} />
          </Field>

          {fieldSet === 'compiled' ? (
            <>
              <div className="subsection-heading">
                <h3>{t('rule.listTitle')}</h3>
                <Button size="small" icon={<PlusOutlined />} aria-label={t('rule.add')} disabled={readOnly} onClick={addRelationRule}>{t('rule.add')}</Button>
              </div>
              <div className="rule-list">
                {relationRules.map((rule) => (rule.type === 'relation_cardinality' ? (
                  <button
                    key={rule.id}
                    type="button"
                    className={`rule-list-item ${selection?.kind === 'constraint' && selection.id === rule.id ? 'is-selected' : ''}`}
                    onClick={() => select?.({ kind: 'constraint', id: rule.id })}
                  >
                    <span className="rule-list-type">{t(`rule.${rule.type}`)}</span>
                    <span className="rule-list-target">{`${rule.min} … ${rule.max === 'unbounded' ? '∞' : rule.max}`}</span>
                  </button>
                ) : null))}
              </div>
            </>
          ) : null}

          {relationExtras ? relationExtras(relation) : null}
        </div>
      ) : constraint ? (
        <div className="properties-content">
          <div className="panel-heading">
            <h2>{t('rule.properties')}</h2>
            <Button danger type="text" icon={<DeleteOutlined />} disabled={readOnly}
              onClick={() => execute({ type: 'constraint.delete', constraintId: constraint.id }, null)} />
          </div>
          <Field label={t('rule.type')}>
            <Select
              disabled={readOnly}
              value={constraint.type}
              onChange={(next) => changeConstraintType(constraint, next)}
              options={RULE_TYPES.map((type) => ({ value: type, label: t(`rule.${type}`) }))}
            />
          </Field>
          {constraint.type === 'relation_cardinality' ? (
            <>
              <Field label={t('rule.relation')}>
                <Select
                  disabled={readOnly}
                  value={constraint.relationId}
                  onChange={(relationId) => {
                    const occupied = document.model.constraints.some(
                      (item) => item.id !== constraint.id && item.type === 'relation_cardinality' && item.relationId === relationId,
                    );
                    if (occupied) {
                      message.warning(t('rule.duplicate'));
                      return;
                    }
                    execute({ type: 'constraint.update', constraintId: constraint.id, constraint: { ...constraint, relationId } });
                  }}
                  options={document.model.relations.map((item) => ({ value: item.id, label: item.name }))}
                />
              </Field>
              <div className="form-row">
                <Field label={t('rule.min')}>
                  <Input type="number" value={constraint.min} disabled={readOnly}
                    onChange={(event) => execute({ type: 'constraint.update', constraintId: constraint.id, constraint: { ...constraint, min: Number(event.target.value) } })} />
                </Field>
                <Field label={t('rule.max')}>
                  <Input value={String(constraint.max)} disabled={readOnly}
                    onChange={(event) => execute({
                      type: 'constraint.update', constraintId: constraint.id,
                      constraint: { ...constraint, max: event.target.value === 'unbounded' ? 'unbounded' : Number(event.target.value) },
                    })} />
                </Field>
              </div>
            </>
          ) : (
            <>
              <Field label={t('rule.entity')}>
                <Select
                  disabled={readOnly}
                  value={constraint.entityId}
                  onChange={(entityId) => {
                    const nextEntity = document.model.entities.find((item) => item.id === entityId);
                    const nextAttribute = nextEntity?.attributes.find((item) =>
                      (constraint.type !== 'value_range' || RANGE_TYPES.includes(item.type))
                      && !document.model.constraints.some(
                        (other) => other.id !== constraint.id && other.type === constraint.type && other.attributeId === item.id,
                      ));
                    if (!nextAttribute) {
                      message.warning(t('rule.noTarget'));
                      return;
                    }
                    execute({ type: 'constraint.update', constraintId: constraint.id, constraint: { ...constraint, entityId, attributeId: nextAttribute.id } as Constraint });
                  }}
                  options={document.model.entities
                    .filter((item) => constraint.type !== 'value_range' || item.attributes.some((attribute) => RANGE_TYPES.includes(attribute.type)))
                    .map((item) => ({ value: item.id, label: item.name }))}
                />
              </Field>
              <Field label={t('rule.attribute')}>
                <Select
                  disabled={readOnly}
                  value={constraint.attributeId}
                  onChange={(attributeId) => {
                    const occupied = document.model.constraints.some(
                      (item) => item.id !== constraint.id && item.type === constraint.type && item.attributeId === attributeId,
                    );
                    if (occupied) {
                      message.warning(t('rule.duplicate'));
                      return;
                    }
                    execute({ type: 'constraint.update', constraintId: constraint.id, constraint: { ...constraint, attributeId } as Constraint });
                  }}
                  options={(document.model.entities.find((item) => item.id === constraint.entityId)?.attributes ?? [])
                    .filter((item) => constraint.type !== 'value_range' || RANGE_TYPES.includes(item.type))
                    .map((item) => ({ value: item.id, label: item.name }))}
                />
              </Field>
              {constraint.type === 'value_range' ? (
                <div className="form-row">
                  <Field label={t('rule.min')}>
                    <Input value={String(constraint.min ?? '')} disabled={readOnly}
                      onChange={(event) => execute({ type: 'constraint.update', constraintId: constraint.id, constraint: { ...constraint, min: Number(event.target.value) } })} />
                  </Field>
                  <Field label={t('rule.max')}>
                    <Input value={String(constraint.max ?? '')} disabled={readOnly}
                      onChange={(event) => execute({ type: 'constraint.update', constraintId: constraint.id, constraint: { ...constraint, max: Number(event.target.value) } })} />
                  </Field>
                </div>
              ) : null}
              {constraint.type === 'query_rule' ? (
                <Field label={t('rule.expression')}>
                  <Input.TextArea
                    className="rule-expression"
                    rows={8}
                    disabled={readOnly}
                    placeholder={t('rule.expressionPlaceholder')}
                    value={constraint.expression}
                    onChange={(event) => execute({
                      type: 'constraint.update', constraintId: constraint.id,
                      constraint: { ...constraint, expression: event.target.value },
                    })}
                  />
                </Field>
              ) : null}
            </>
          )}
        </div>
      ) : (
        <div className="properties-empty">
          <div>
            <h3>{t('selection.noneTitle')}</h3>
            <p>{t('selection.noneBody')}</p>
          </div>
        </div>
      )}
    </aside>
  );
}
