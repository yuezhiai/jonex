import { normalizeTechnicalName } from './normalize.js';
import type { Attribute, Constraint, DomainDocument, EntityPatch, EntityType, RelationPatch, RelationType } from './types.js';

/** 命令被拒时携带的问题码。原实现复用 AI 提案的 issue 类型；本移植不带提案，
 *  所以自持一个最小的类型——调用方只读 `code`/`params`。 */
export interface DomainCommandIssue {
  code: string;
  params?: Record<string, string | number>;
}

export class DomainCommandError extends Error {
  constructor(public readonly issues: DomainCommandIssue[]) {
    super('The domain command could not be applied safely');
    this.name = 'DomainCommandError';
  }
}

export type DomainCommand =
  | { type: 'entity.add'; entity: EntityType; position: { x: number; y: number } }
  | { type: 'entity.update'; entityId: string; changes: Partial<{ [K in Exclude<keyof EntityPatch, 'attributes'>]: EntityPatch[K] }> }
  | { type: 'entity.delete'; entityId: string }
  | { type: 'attribute.add'; entityId: string; attribute: Attribute }
  | { type: 'attribute.update'; entityId: string; attributeId: string; changes: Partial<Omit<Attribute, 'id'>> }
  | { type: 'attribute.delete'; entityId: string; attributeId: string }
  | { type: 'attribute.reorder'; entityId: string; attributeId: string; direction: 'up' | 'down' }
  | { type: 'relation.add'; relation: RelationType }
  | { type: 'relation.update'; relationId: string; changes: RelationPatch }
  | { type: 'relation.delete'; relationId: string }
  | { type: 'constraint.add'; constraint: Constraint }
  | { type: 'constraint.update'; constraintId: string; constraint: Constraint }
  | { type: 'constraint.delete'; constraintId: string }
  | { type: 'node.move'; entityId: string; position: { x: number; y: number } }
  | { type: 'layout.apply'; positions: Record<string, { x: number; y: number }> }
  | { type: 'viewport.set'; viewport: { x: number; y: number; zoom: number } }
  | { type: 'canvas.clear' };

export function applyCommand(document: DomainDocument, command: DomainCommand): DomainDocument {
  const next = structuredClone(document);
  const model = next.model;
  switch (command.type) {
    case 'entity.add':
      if (model.entities.some((item) => item.name.toLowerCase() === command.entity.name.toLowerCase())) return next;
      model.entities.push(command.entity);
      next.editor.nodePositions[command.entity.id] = command.position;
      break;
    case 'entity.update': {
      const entity = model.entities.find((item) => item.id === command.entityId);
      if (command.changes.name && model.entities.some((item) => item.id !== command.entityId && item.name.toLowerCase() === command.changes.name?.toLowerCase())) return next;
      if (entity) Object.assign(entity, command.changes);
      break;
    }
    case 'entity.delete': {
      const attributeIds = new Set(model.entities.find((item) => item.id === command.entityId)?.attributes.map((item) => item.id) ?? []);
      const relationIds = new Set(model.relations.filter((item) => item.sourceEntityId === command.entityId || item.targetEntityId === command.entityId).map((item) => item.id));
      model.entities = model.entities.filter((item) => item.id !== command.entityId);
      // 关系:端点命中即整条删;其余关系的多态端点集合里也摘掉该实体(否则导出时名字解析会抛)
      model.relations = model.relations
        .filter((item) => !relationIds.has(item.id))
        .map((item) => {
          const drop = (ids: string[] | undefined): string[] | undefined => {
            if (ids === undefined) return undefined;
            const kept = ids.filter((id) => id !== command.entityId);
            return kept.length > 0 ? kept : undefined;
          };
          const sourceTypes = drop(item.sourceTypes);
          const targetTypes = drop(item.targetTypes);
          const next: RelationType = { ...item };
          if (sourceTypes === undefined) delete next.sourceTypes; else next.sourceTypes = sourceTypes;
          if (targetTypes === undefined) delete next.targetTypes; else next.targetTypes = targetTypes;
          return next;
        });
      model.constraints = model.constraints.filter((item) => item.type === 'relation_cardinality' ? !relationIds.has(item.relationId) : item.entityId !== command.entityId && !attributeIds.has(item.attributeId));
      delete next.editor.nodePositions[command.entityId];
      break;
    }
    case 'attribute.add': {
      const entity = model.entities.find((item) => item.id === command.entityId);
      if (entity && !entity.attributes.some((item) => item.name.toLowerCase() === command.attribute.name.toLowerCase())) entity.attributes.push(command.attribute);
      break;
    }
    case 'attribute.update': {
      const entity = model.entities.find((item) => item.id === command.entityId);
      const attribute = entity?.attributes.find((item) => item.id === command.attributeId);
      if (command.changes.name && entity?.attributes.some((item) => item.id !== command.attributeId && item.name.toLowerCase() === command.changes.name?.toLowerCase())) return next;
      if (attribute) Object.assign(attribute, command.changes);
      break;
    }
    case 'attribute.delete': {
      const entity = model.entities.find((item) => item.id === command.entityId);
      if (entity) entity.attributes = entity.attributes.filter((item) => item.id !== command.attributeId);
      model.constraints = model.constraints.filter((item) => item.type === 'relation_cardinality' || item.attributeId !== command.attributeId);
      break;
    }
    case 'attribute.reorder': {
      const attributes = model.entities.find((item) => item.id === command.entityId)?.attributes;
      if (!attributes) break;
      const from = attributes.findIndex((item) => item.id === command.attributeId);
      const to = command.direction === 'up' ? from - 1 : from + 1;
      if (from >= 0 && to >= 0 && to < attributes.length) [attributes[from], attributes[to]] = [attributes[to]!, attributes[from]!];
      break;
    }
    case 'relation.add':
      if (!model.relations.some((item) => item.name.toLowerCase() === command.relation.name.toLowerCase())) model.relations.push(command.relation);
      break;
    case 'relation.update': {
      const relation = model.relations.find((item) => item.id === command.relationId);
      if (command.changes.name && model.relations.some((item) => item.id !== command.relationId && item.name.toLowerCase() === command.changes.name?.toLowerCase())) return next;
      if (relation) Object.assign(relation, command.changes);
      break;
    }
    case 'relation.delete':
      model.relations = model.relations.filter((item) => item.id !== command.relationId);
      model.constraints = model.constraints.filter((item) => item.type !== 'relation_cardinality' || item.relationId !== command.relationId);
      break;
    case 'constraint.add': {
      const duplicate = model.constraints.some((item) => item.type === command.constraint.type && (item.type === 'relation_cardinality' && command.constraint.type === 'relation_cardinality' ? item.relationId === command.constraint.relationId : item.type !== 'relation_cardinality' && command.constraint.type !== 'relation_cardinality' && item.attributeId === command.constraint.attributeId));
      if (!duplicate) model.constraints.push(command.constraint);
      break;
    }
    case 'constraint.update': {
      const index = model.constraints.findIndex((item) => item.id === command.constraintId);
      if (index >= 0) model.constraints[index] = command.constraint;
      break;
    }
    case 'constraint.delete':
      model.constraints = model.constraints.filter((item) => item.id !== command.constraintId);
      break;
    case 'node.move':
      next.editor.nodePositions[command.entityId] = command.position;
      break;
    case 'layout.apply':
      next.editor.nodePositions = structuredClone(command.positions);
      break;
    case 'viewport.set':
      next.editor.viewport = command.viewport;
      break;
    case 'canvas.clear':
      model.entities = [];
      model.relations = [];
      model.constraints = [];
      next.editor.nodePositions = {};
      break;
  }
  return next;
}
