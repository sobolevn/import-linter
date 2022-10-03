import copy
from typing import Iterator, List, Optional, Tuple, Union, cast

from typing_extensions import TypedDict

from importlinter.application import contract_utils, output
from importlinter.application.contract_utils import AlertLevel
from importlinter.configuration import settings
from importlinter.domain import fields
from importlinter.domain.contract import Contract, ContractCheck
from importlinter.domain.imports import Module
from importlinter.domain.ports.graph import ImportGraph

from ._common import Chain, DetailedChain, Link, render_chain_data


class Layer:
    def __init__(self, name: str, is_optional: bool = False) -> None:
        self.name = name
        self.is_optional = is_optional


class LayerField(fields.Field):
    def parse(self, raw_data: Union[str, List]) -> Layer:
        raw_string = fields.StringField().parse(raw_data)
        if raw_string.startswith("(") and raw_string.endswith(")"):
            layer_name = raw_string[1:-1]
            is_optional = True
        else:
            layer_name = raw_string
            is_optional = False
        return Layer(name=layer_name, is_optional=is_optional)


class _LayerChainData(TypedDict):
    higher_layer: str
    lower_layer: str
    chains: List[DetailedChain]


class LayersContract(Contract):
    """
    Defines a 'layered architecture' where there is a unidirectional dependency flow.

    Specifically, higher layers may depend on lower layers, but not the other way around.
    To allow for a repeated pattern of layers across a project, you may also define a set of
    'containers', which are treated as the parent package of the layers.

    Layers are required by default: if a layer is listed in the contract, the contract will be
    broken if the layer doesn’t exist. You can make a layer optional by wrapping it in parentheses.

    Configuration options:

        - layers:         An ordered list of layers. Each layer is the name of a module relative
                          to its parent package. The order is from higher to lower level layers.
        - containers:     A list of the parent Modules of the layers (optional).
        - ignore_imports: A set of ImportExpressions. These imports will be ignored: if the import
                          would cause a contract to be broken, adding it to the set will cause
                          the contract be kept instead. (Optional.)
        - unmatched_ignore_imports_alerting: Decides how to report when the expression in the
                          `ignore_imports` set is not found in the graph. Valid values are
                          "none", "warn", "error". Default value is "error".
    """

    type_name = "layers"

    layers = fields.ListField(subfield=LayerField())
    containers = fields.ListField(subfield=fields.StringField(), required=False)
    ignore_imports = fields.SetField(subfield=fields.ImportExpressionField(), required=False)
    unmatched_ignore_imports_alerting = fields.EnumField(AlertLevel, default=AlertLevel.ERROR)

    def check(self, graph: ImportGraph, verbose: bool) -> ContractCheck:
        is_kept = True
        invalid_chains = []

        warnings = contract_utils.remove_ignored_imports(
            graph=graph,
            ignore_imports=self.ignore_imports,  # type: ignore
            unmatched_alerting=self.unmatched_ignore_imports_alerting,  # type: ignore
        )

        if self.containers:
            self._validate_containers(graph)
        else:
            self._check_all_containerless_layers_exist(graph)

        for (
            higher_layer_package,
            lower_layer_package,
            container,
        ) in self._generate_module_permutations(graph):
            output.verbose_print(
                verbose,
                "Searching for import chains from "
                f"{lower_layer_package} to {higher_layer_package}...",
            )
            with settings.TIMER as timer:
                layer_chain_data = self._build_layer_chain_data(
                    higher_layer_package=higher_layer_package,
                    lower_layer_package=lower_layer_package,
                    container=container,
                    graph=graph,
                )

            if layer_chain_data["chains"]:
                is_kept = False
                invalid_chains.append(layer_chain_data)
            if verbose:
                chain_count = len(layer_chain_data["chains"])
                pluralized = "s" if chain_count != 1 else ""
                output.print(
                    f"Found {chain_count} illegal chain{pluralized} "
                    f"in {timer.duration_in_s}s.",
                )

        return ContractCheck(
            kept=is_kept, warnings=warnings, metadata={"invalid_chains": invalid_chains}
        )

    def render_broken_contract(self, check: ContractCheck) -> None:
        for chains_data in cast(List[_LayerChainData], check.metadata["invalid_chains"]):
            higher_layer, lower_layer = (chains_data["higher_layer"], chains_data["lower_layer"])
            output.print(f"{lower_layer} is not allowed to import {higher_layer}:")
            output.new_line()

            for chain_data in chains_data["chains"]:
                render_chain_data(chain_data)
                output.new_line()

            output.new_line()

    def _validate_containers(self, graph: ImportGraph) -> None:
        root_package_names = self.session_options["root_packages"]
        root_packages = tuple(Module(name) for name in root_package_names)

        for container in self.containers:  # type: ignore
            if not any(
                Module(container).is_in_package(root_package) for root_package in root_packages
            ):
                if len(root_package_names) == 1:
                    root_package_name = root_package_names[0]
                    error_message = (
                        f"Invalid container '{container}': a container must either be a "
                        f"subpackage of {root_package_name}, or {root_package_name} itself."
                    )
                else:
                    packages_string = ", ".join(root_package_names)
                    error_message = (
                        f"Invalid container '{container}': a container must either be a root "
                        f"package, or a subpackage of one of them. "
                        f"(The root packages are: {packages_string}.)"
                    )
                raise ValueError(error_message)
            self._check_all_layers_exist_for_container(container, graph)

    def _check_all_layers_exist_for_container(self, container: str, graph: ImportGraph) -> None:
        for layer in self.layers:  # type: ignore
            if layer.is_optional:
                continue
            layer_module_name = ".".join([container, layer.name])
            if layer_module_name not in graph.modules:
                raise ValueError(
                    f"Missing layer in container '{container}': "
                    f"module {layer_module_name} does not exist."
                )

    def _check_all_containerless_layers_exist(self, graph: ImportGraph) -> None:
        for layer in self.layers:  # type: ignore
            if layer.is_optional:
                continue
            if layer.name not in graph.modules:
                raise ValueError(
                    f"Missing layer '{layer.name}': module {layer.name} does not exist."
                )

    def _generate_module_permutations(
        self, graph: ImportGraph
    ) -> Iterator[Tuple[Module, Module, Optional[str]]]:
        """
        Return all possible combinations of higher level and lower level modules, in pairs.

        Each pair of modules consists of immediate children of two different layers. The first
        module is in a layer higher than the layer of the second module. This means the first
        module is allowed to import the second, but not the other way around.

        Returns:
            module_in_higher_layer, module_in_lower_layer, container
        """
        # If there are no containers, we still want to run the loop once.
        quasi_containers = self.containers or [None]  # type: ignore

        for container in quasi_containers:  # type: ignore
            for index, higher_layer in enumerate(self.layers):  # type: ignore
                higher_layer_module = self._module_from_layer(higher_layer, container)

                if higher_layer_module.name not in graph.modules:
                    continue

                for lower_layer in self.layers[index + 1 :]:  # type: ignore

                    lower_layer_module = self._module_from_layer(lower_layer, container)

                    if lower_layer_module.name not in graph.modules:
                        continue

                    yield higher_layer_module, lower_layer_module, container

    def _module_from_layer(self, layer: Layer, container: Optional[str] = None) -> Module:
        if container:
            name = ".".join([container, layer.name])
        else:
            name = layer.name
        return Module(name)

    def _build_layer_chain_data(
        self,
        higher_layer_package: Module,
        lower_layer_package: Module,
        container: Optional[str],
        graph: ImportGraph,
    ) -> _LayerChainData:
        """
        Build a dictionary of illegal chains between two layers, in the form:

            higher_layer (str): Higher layer package name.
            lower_layer (str):  Lower layer package name.
            chains (list):      List of <detailed chain> lists.
        """
        temp_graph = copy.deepcopy(graph)
        self._remove_other_layers(
            temp_graph,
            container=container,
            layers_to_preserve=(higher_layer_package, lower_layer_package),
        )
        # Assemble direct imports between the layers, then remove them.
        import_details_between_layers = self._pop_direct_imports(
            higher_layer_package=higher_layer_package,
            lower_layer_package=lower_layer_package,
            graph=temp_graph,
        )
        collapsed_direct_chains: List[DetailedChain] = []
        for import_details_list in import_details_between_layers:
            line_numbers = tuple(j["line_number"] for j in import_details_list)
            collapsed_direct_chains.append(
                {
                    "chain": [
                        {
                            "importer": import_details_list[0]["importer"],
                            "imported": import_details_list[0]["imported"],
                            "line_numbers": line_numbers,
                        }
                    ],
                    "extra_firsts": [],
                    "extra_lasts": [],
                }
            )

        layer_chain_data: _LayerChainData = {
            "higher_layer": higher_layer_package.name,
            "lower_layer": lower_layer_package.name,
            "chains": collapsed_direct_chains,  # type: ignore
        }

        indirect_chain_data = self._get_indirect_collapsed_chains(
            temp_graph, importer_package=lower_layer_package, imported_package=higher_layer_package
        )
        layer_chain_data["chains"].extend(indirect_chain_data)  # type: ignore

        return layer_chain_data

    @classmethod
    def _get_indirect_collapsed_chains(
        cls, graph: ImportGraph, importer_package: Module, imported_package: Module
    ) -> List[DetailedChain]:
        """
        Squashes the two packages.
        Gets a list of paths between them, called segments.
        Add the heads and tails to the segments.
        Return a list of detailed chains in the following format:

        [
            {
                "chain": <detailed chain>,
                "extra_firsts": [
                    <import details>,
                    ...
                ],
                "extra_lasts": [
                    <import details>,
                    <import details>,
                    ...
                ],
            }
        ]
        """
        temp_graph = copy.deepcopy(graph)

        temp_graph.squash_module(importer_package.name)
        temp_graph.squash_module(imported_package.name)

        segments = cls._find_segments(
            temp_graph, reference_graph=graph, importer=importer_package, imported=imported_package
        )
        return cls._segments_to_collapsed_chains(
            graph, segments, importer=importer_package, imported=imported_package
        )

    @classmethod
    def _find_segments(
        cls, graph: ImportGraph, reference_graph: ImportGraph, importer: Module, imported: Module
    ) -> List[Chain]:
        """
        Return list of headless and tailless chains.

        Two graphs are passed in: the first is mutated, the second is used purely as a reference to
        look up import details which are otherwise removed during mutation.
        """
        segments = []
        for chain in cls._pop_shortest_chains(
            graph, importer=importer.name, imported=imported.name
        ):
            if len(chain) == 2:
                raise ValueError("Direct chain found - these should have been removed.")
            segment: List[Link] = []
            for importer_in_chain, imported_in_chain in [
                (chain[i], chain[i + 1]) for i in range(len(chain) - 1)
            ]:
                import_details = reference_graph.get_import_details(
                    importer=importer_in_chain, imported=imported_in_chain
                )
                line_numbers = tuple(set(cast(int, j["line_number"]) for j in import_details))
                segment.append(
                    {
                        "importer": importer_in_chain,
                        "imported": imported_in_chain,
                        "line_numbers": line_numbers,
                    }
                )
            segments.append(segment)
        return segments

    @classmethod
    def _pop_shortest_chains(cls, graph: ImportGraph, importer: str, imported: str):
        chain: Union[Optional[Tuple[str, ...]], bool] = True
        while chain:
            chain = graph.find_shortest_chain(importer, imported)
            if chain:
                # Remove chain of imports from graph.
                for index in range(len(chain) - 1):
                    graph.remove_import(importer=chain[index], imported=chain[index + 1])
                yield chain

    @classmethod
    def _segments_to_collapsed_chains(
        cls, graph: ImportGraph, segments: List[Chain], importer: Module, imported: Module
    ) -> List[DetailedChain]:
        collapsed_chains: List[DetailedChain] = []
        for segment in segments:
            head_imports: List[Link] = []
            imported_module = segment[0]["imported"]
            candidate_modules = sorted(graph.find_modules_that_directly_import(imported_module))
            for module in [
                m
                for m in candidate_modules
                if Module(m) == importer or Module(m).is_descendant_of(importer)
            ]:
                import_details_list = graph.get_import_details(
                    importer=module, imported=imported_module
                )
                line_numbers = tuple(set(cast(int, j["line_number"]) for j in import_details_list))
                head_imports.append(
                    {"importer": module, "imported": imported_module, "line_numbers": line_numbers}
                )

            tail_imports: List[Link] = []
            importer_module = segment[-1]["importer"]
            candidate_modules = sorted(graph.find_modules_directly_imported_by(importer_module))
            for module in [
                m
                for m in candidate_modules
                if Module(m) == imported or Module(m).is_descendant_of(imported)
            ]:
                import_details_list = graph.get_import_details(
                    importer=importer_module, imported=module
                )
                line_numbers = tuple(set(cast(int, j["line_number"]) for j in import_details_list))
                tail_imports.append(
                    {"importer": importer_module, "imported": module, "line_numbers": line_numbers}
                )

            collapsed_chains.append(
                {
                    "chain": [head_imports[0]] + segment[1:-1] + [tail_imports[0]],
                    "extra_firsts": head_imports[1:],
                    "extra_lasts": tail_imports[1:],
                }
            )

        return collapsed_chains

    def _remove_other_layers(self, graph: ImportGraph, container, layers_to_preserve):
        for index, layer in enumerate(self.layers):  # type: ignore
            candidate_layer = self._module_from_layer(layer, container)
            if candidate_layer.name in graph.modules and candidate_layer not in layers_to_preserve:
                self._remove_layer(graph, layer_package=candidate_layer)

    def _remove_layer(self, graph: ImportGraph, layer_package):
        for module in graph.find_descendants(layer_package.name):
            graph.remove_module(module)
        graph.remove_module(layer_package.name)

    @classmethod
    def _pop_direct_imports(cls, higher_layer_package, lower_layer_package, graph: ImportGraph):
        import_details_list = []
        lower_layer_modules = {lower_layer_package.name} | graph.find_descendants(
            lower_layer_package.name
        )
        for lower_layer_module in lower_layer_modules:
            imported_modules = graph.find_modules_directly_imported_by(lower_layer_module).copy()
            for imported_module in imported_modules:
                if Module(imported_module) == higher_layer_package or Module(
                    imported_module
                ).is_descendant_of(higher_layer_package):
                    import_details = graph.get_import_details(
                        importer=lower_layer_module, imported=imported_module
                    )
                    if not import_details:
                        # get_import_details may not return any imports (for example if an import
                        # has been added without metadata. If nothing is returned, we still want
                        # to add some details about the import to the list.
                        import_details = [
                            {
                                "importer": lower_layer_module,
                                "imported": imported_module,
                                "line_number": "?",
                                "line_contents": "",
                            }
                        ]
                    import_details_list.append(import_details)
                    graph.remove_import(importer=lower_layer_module, imported=imported_module)
        return import_details_list
