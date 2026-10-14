"""Lossless source locations with one copy of each passage's metadata."""
from collections.abc import Mapping
import hashlib,json


def compact_source_index(records, locations):
    metadata, compact, owners, meta_owners = {}, {}, {}, {}
    for path, rows in records.items():
        owners[path] = "|".join(sorted({row['document_id'] for row in rows if row.get('document_id')}))
    for path, location in locations.items():
        rows = records.get(path, ())
        passage = rows[0].get('passage_id') if rows else None
        if passage and isinstance(location.get('metadata'), dict):
            value = location['metadata']
            if passage in metadata and metadata[passage] != value:
                passage += ':'+hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            metadata[passage] = value
            meta_owners[passage] = owners.get(path, '')
            compact[path] = {'location':{k:v for k,v in location.items() if k != 'metadata'}, 'metadata_ref': passage}
        else:
            compact[path] = {'location':location}
    return ({'schema_version':3,'source_records':records,'source_locations':compact,
             'passage_metadata':metadata},
            {'source_records':owners,'source_locations':owners,'passage_metadata':meta_owners})


class SourceLocations(Mapping):
    def __init__(self, locations, metadata):
        self.locations, self.metadata = locations, metadata

    def __iter__(self):return iter(self.locations)
    def __len__(self):return len(self.locations)
    def __contains__(self,key):return key in self.locations
    def __getitem__(self, key):
        value = self.locations[key]
        if not isinstance(value,dict):raise ValueError('invalid source location')
        location=value.get('location')
        if not isinstance(location,dict):raise ValueError('invalid source location payload')
        if 'metadata_ref' not in value:return location
        reference = value['metadata_ref']
        metadata = self.metadata[reference]
        if not isinstance(metadata, dict):raise ValueError('invalid source metadata')
        return {**location, 'metadata':metadata}


def restore_source_index(payload):
    if payload.get('schema_version') == 3:
        payload = {**payload, 'source_locations':SourceLocations(
            payload['source_locations'], payload['passage_metadata'])}
    return payload
