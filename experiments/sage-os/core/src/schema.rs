//! Checks a tool call's input against the tool's `inputSchema`.

use serde_json::Value;

/// What is wrong with `value` under `schema`, if anything.
///
/// ponytail: a JSON Schema subset (type, enum, required, properties,
/// additionalProperties: false, items); other keywords are ignored. Add them when a
/// device declares one that matters.
pub fn check(schema: &Value, value: &Value, at: &str) -> Result<(), String> {
    let Some(schema) = schema.as_object() else {
        return Ok(());
    };
    if let Some(kind) = schema.get("type").and_then(Value::as_str) {
        let fits = match kind {
            "object" => value.is_object(),
            "array" => value.is_array(),
            "string" => value.is_string(),
            "boolean" => value.is_boolean(),
            "null" => value.is_null(),
            "number" => value.is_number(),
            "integer" => {
                value.is_i64() || value.is_u64() || value.as_f64().is_some_and(|f| f.fract() == 0.0)
            }
            _ => true,
        };
        if !fits {
            return Err(format!("{at} must be of type {kind}"));
        }
    }
    if let Some(options) = schema.get("enum").and_then(Value::as_array)
        && !options.contains(value)
    {
        return Err(format!(
            "{at} must be one of {}",
            Value::Array(options.clone())
        ));
    }
    if let Some(object) = value.as_object() {
        for key in schema
            .get("required")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            if !object.contains_key(key) {
                return Err(format!("{at}.{key} is required"));
            }
        }
        let properties = schema.get("properties").and_then(Value::as_object);
        for (key, item) in object {
            match properties.and_then(|p| p.get(key)) {
                Some(sub) => check(sub, item, &format!("{at}.{key}"))?,
                None if schema.get("additionalProperties") == Some(&Value::Bool(false)) => {
                    return Err(format!("{at}.{key} is not allowed"));
                }
                None => {}
            }
        }
    }
    if let (Some(items), Some(list)) = (schema.get("items"), value.as_array()) {
        for (index, item) in list.iter().enumerate() {
            check(items, item, &format!("{at}[{index}]"))?;
        }
    }
    Ok(())
}
