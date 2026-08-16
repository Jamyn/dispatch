"""
CompositeSearch provides search across multiple tables
with the connection objects.


Base usage::

    s = CompositeSearch(session, [User, Comment, Blog])
    q = s.build_query('star wars', sort=True).limit(10)
    s.search(query=q)


Adding other objects::

    class RatingSearch(CompositeSearch):

        def extend_search_objects(self, model_class, objects):
            part = Rating.query.filter(
                Rating.author_id == request.user_id,
                Rating.target.is_type(model_class),
                Rating.target_id.in_(objects.keys()))
            part = {x.target_id: x for x in part}
            for k, v in objects.items():
                objects[k] = (v, part.get(k))
            return objects

        def map_result(self, search_row, object):
            content, rating = object
            obj = {'type': search_row.type, 'content': content}
            return {'object': obj, 'rating': rating}

    s = RatingSearch(session, [User, Comment, Blog])
    q = s.build_query('star wars', sort=True).limit(10)
    s.search(query=q)

"""

from collections import defaultdict
from sqlalchemy import desc, func, union
from sqlalchemy.sql.expression import literal

from . import inspect_search_vectors, search_manager


class CompositeSearch(object):
    def __init__(self, session, model_classes):
        self.session = session
        self.model_classes = model_classes

    def union_query(self, search_query):
        """Matches and ranks each model under its own regconfig.

        The union collapses vectors from models that declare different
        regconfigs into one column, so a single tsquery applied afterwards is
        necessarily wrong for some arm. Both the predicate and the rank are
        therefore built per model, before the union.

        Returns a selectable, not a Query. All arms are combined in one n-ary
        union: folding them pairwise nests each union inside the next, and from
        the third arm on the nested arm's columns are renamed while the new
        arm's are not, so the two stop corresponding and the combined columns
        lose their names entirely.
        """
        arms = []
        for model_class in self.model_classes:
            search_vectors = inspect_search_vectors(model_class)
            vector = search_vectors[0]
            regconfig = search_manager.option(vector, "regconfig")
            tsquery = func.tsq_parse(regconfig, search_query)
            arms.append(
                self.session.query(
                    model_class.id.label("id"),
                    vector.label("vector"),
                    literal(model_class.__name__).label("type"),
                    func.ts_rank_cd(vector, tsquery).label("rank"),
                )
                .filter(vector.op("@@")(tsquery))
                .statement
            )
        return union(*arms)

    def build_query(self, search_query, sort=False):
        """Selects from the union, ranked across every arm.

        Ordering is applied to the wrapping select, on the subquery's own rank
        column, so the ordering term is a column the outer scope exports rather
        than a per-arm label addressed by name from outside the union.

        `rank` alone does not order anything totally: `ts_rank_cd` returns a
        float4, and every row matching once in one indexed column scores the
        same value, so ranked ties are the ordinary case. Postgres' sort is not
        stable, so `(type, id)` breaks them -- without it two identical searches
        may return the tied rows in different orders, and any LIMIT/OFFSET over
        the result would drop rows and repeat others across pages (#160).
        """
        combined = self.union_query(search_query).subquery()
        qs = self.session.query(combined.c.id, combined.c.vector, combined.c.type, combined.c.rank)
        if sort:
            qs = qs.order_by(desc(combined.c.rank), combined.c.type, combined.c.id)
        return qs

    def split_filter(self, model_class, obj):
        return obj.type == model_class.__name__

    def split_search_result(self, search_result):
        objects_by_model = {x: [] for x in self.model_classes}
        for x in search_result:
            for model_class in self.model_classes:
                if self.split_filter(model_class, x):
                    objects_by_model[model_class].append(x)
        return objects_by_model

    def extend_search_objects(self, model_class, objects):
        return objects

    def load_search_objects(self, objects_by_model):
        objects_by_type = {x.__name__: [] for x in self.model_classes}
        for model_class, objects in objects_by_model.items():
            if objects:
                objects = {
                    x.id: x
                    for x in self.session.query(model_class).filter(
                        model_class.id.in_([x.id for x in objects])
                    )
                }
                objects = self.extend_search_objects(model_class, objects)
            objects_by_type[model_class.__name__] = objects
        return objects_by_type

    def map_result(self, search_row, object):
        return {"type": search_row.type, "content": object}

    def search(self, query, by_type=True):
        search_result = list(query)
        objects_by_model = self.split_search_result(search_result)
        objects_by_type = self.load_search_objects(objects_by_model)

        # mapping all to search result
        objects = defaultdict(list)
        if by_type:
            for x in search_result:
                if x.type in objects_by_type:
                    objects[x.type].append(objects_by_type[x.type][x.id])
        return objects
