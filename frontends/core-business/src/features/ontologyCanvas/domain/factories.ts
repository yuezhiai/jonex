import type { Constraint, DomainDocument, DomainSummary, EntityType, OntologyModel } from './types.js';
import { DomainDocumentSchema } from './schemas.js';
export { newId } from './ids.js';
import { newId } from './ids.js';

export function createEmptyDocument(input: { id: string; displayName: string; description?: string }, now = new Date().toISOString()): DomainDocument {
  return {
    storageVersion: 1,
    schemaVersion: 1,
    revision: 0,
    domain: { id: input.id, displayName: input.displayName, ...(input.description ? { description: input.description } : {}) },
    model: {
      entities: [],
      relations: [],
      constraints: [],
      disambiguation: { caseInsensitive: true, aliasMerge: true },
    },
    editor: { nodePositions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
    savedAt: now,
  };
}

export function summarizeDocument(document: DomainDocument): DomainSummary {
  return {
    id: document.domain.id,
    displayName: document.domain.displayName,
    ...(document.domain.description ? { description: document.domain.description } : {}),
    schemaVersion: 1,
    entityCount: document.model.entities.length,
    relationCount: document.model.relations.length,
    ruleCount: document.model.constraints.length,
    updatedAt: document.savedAt,
    revision: document.revision,
  };
}

export function cloneDocumentForDomain(source: DomainDocument, input: { id: string; displayName: string }, now = new Date().toISOString()): DomainDocument {
  const entityIds = new Map<string, string>();
  const attributeIds = new Map<string, string>();
  const relationIds = new Map<string, string>();

  const entities: EntityType[] = source.model.entities.map((entity) => {
    const entityId = newId();
    entityIds.set(entity.id, entityId);
    return {
      ...structuredClone(entity),
      id: entityId,
      attributes: entity.attributes.map((attribute) => {
        const attributeId = newId();
        attributeIds.set(attribute.id, attributeId);
        return { ...structuredClone(attribute), id: attributeId };
      }),
    };
  });

  const relations = source.model.relations.map((relation) => {
    const relationId = newId();
    relationIds.set(relation.id, relationId);
    return {
      ...structuredClone(relation),
      id: relationId,
      sourceEntityId: entityIds.get(relation.sourceEntityId) ?? relation.sourceEntityId,
      targetEntityId: entityIds.get(relation.targetEntityId) ?? relation.targetEntityId,
    };
  });

  const constraints: Constraint[] = source.model.constraints.map((constraint) => {
    const copy = { ...structuredClone(constraint), id: newId() };
    if (copy.type === 'relation_cardinality') {
      return { ...copy, relationId: relationIds.get(copy.relationId) ?? copy.relationId };
    }
    return {
      ...copy,
      entityId: entityIds.get(copy.entityId) ?? copy.entityId,
      attributeId: attributeIds.get(copy.attributeId) ?? copy.attributeId,
    };
  });

  const nodePositions = Object.fromEntries(
    Object.entries(source.editor.nodePositions).flatMap(([oldId, position]) => {
      const id = entityIds.get(oldId);
      return id ? [[id, { ...position }]] : [];
    }),
  );

  return {
    ...structuredClone(source),
    revision: 0,
    domain: { id: input.id, displayName: input.displayName, ...(source.domain.description ? { description: source.domain.description } : {}) },
    model: { ...structuredClone(source.model), entities, relations, constraints },
    editor: { nodePositions, viewport: { ...source.editor.viewport } },
    savedAt: now,
  };
}

/**
 * 模型 + 域身份 → 新文档。**建域形状的唯一出处** —— `deserializeSchema`(YAML 导入)与
 * RDF 导入共用它,免得「四列网格」「revision 0」这类形状有两份实现、慢慢分叉。
 */
export function buildDocumentFromModel(
  model: OntologyModel,
  domain: { id: string; displayName: string; description?: string | undefined },
  now = new Date().toISOString(),
): DomainDocument {
  const nodePositions = Object.fromEntries(
    model.entities.map((entity, index) => [entity.id, { x: (index % 4) * 280, y: Math.floor(index / 4) * 180 }]),
  );
  return DomainDocumentSchema.parse({
    storageVersion: 1,
    schemaVersion: 1,
    revision: 0,
    domain: { id: domain.id, displayName: domain.displayName, ...(domain.description !== undefined ? { description: domain.description } : {}) },
    model,
    editor: { nodePositions, viewport: { x: 0, y: 0, zoom: 1 } },
    savedAt: now,
  }) as DomainDocument;
}
