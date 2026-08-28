-- 提示词模板版本号从 X.Y 格式统一为纯整数格式（v1 → v2 → v3）
-- 存量模板的 versions_json 均只有一条「初始版本」记录，统一归为 v1
BEGIN;

UPDATE business_domain.prompt_templates
SET current_version = '1';

UPDATE business_domain.prompt_templates
SET versions_json = (
    SELECT jsonb_agg(jsonb_set(v, '{version}', '"1"'::jsonb))
    FROM jsonb_array_elements(versions_json) AS v
)
WHERE versions_json IS NOT NULL AND jsonb_array_length(versions_json) > 0;

COMMIT;
