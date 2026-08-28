-- 模板领域新增英文名 name_en：支持改名 + 保留英文翻译
-- 背景：内置领域此前由前端 i18n 硬编码显示英文名，改名不生效；
--       改为 DB 存 name_en，前端按语言显示。name 字段语义不变。

ALTER TABLE business_domain.template_domains
    ADD COLUMN IF NOT EXISTS name_en VARCHAR(255);

-- 回填 6 个内置领域的英文名（取自 en.json templateDomains.builtIn.*.name）
UPDATE business_domain.template_domains SET name_en = 'Internet Technology'   WHERE id = 'tpl_domain_internet'          AND name_en IS NULL;
UPDATE business_domain.template_domains SET name_en = 'Financial Services'    WHERE id = 'tpl_domain_finance'           AND name_en IS NULL;
UPDATE business_domain.template_domains SET name_en = 'Healthcare'            WHERE id = 'tpl_domain_medical'           AND name_en IS NULL;
UPDATE business_domain.template_domains SET name_en = 'Manufacturing'         WHERE id = 'tpl_domain_manufacturing'     AND name_en IS NULL;
UPDATE business_domain.template_domains SET name_en = 'Hardware & Internet'   WHERE id = 'tpl_domain_hardware_internet' AND name_en IS NULL;
UPDATE business_domain.template_domains SET name_en = 'LLM Technical Reports' WHERE id = 'tpl_domain_ai_tech_report'    AND name_en IS NULL;
