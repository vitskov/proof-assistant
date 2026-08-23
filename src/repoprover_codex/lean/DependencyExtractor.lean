import Lean
import Lean.Util.CollectAxioms
import Lean.Util.FoldConsts
import Formalization.All

open Lean

namespace RepoProverDependencyExtractor

private partial def encodeLevel : Level → Json
  | .zero => Json.arr #["zero"]
  | .succ level => Json.arr #["succ", encodeLevel level]
  | .max left right => Json.arr #["max", encodeLevel left, encodeLevel right]
  | .imax left right => Json.arr #["imax", encodeLevel left, encodeLevel right]
  | .param name => Json.arr #["param", name.toString]
  | .mvar identifier => Json.arr #["mvar", reprStr identifier]

private def encodeLevels (levels : List Level) : Json :=
  Json.arr <| levels.toArray.map encodeLevel

private partial def encodeExpr : Expr → Json
  | .bvar index => Json.arr #["bvar", toJson index]
  | .fvar identifier => Json.arr #["fvar", reprStr identifier]
  | .mvar identifier => Json.arr #["mvar", reprStr identifier]
  | .sort level => Json.arr #["sort", encodeLevel level]
  | .const name levels => Json.arr #["const", name.toString, encodeLevels levels]
  | .app function argument => Json.arr #["app", encodeExpr function, encodeExpr argument]
  | .lam _ type body binderInfo =>
      Json.arr #["lam", reprStr binderInfo, encodeExpr type, encodeExpr body]
  | .forallE _ type body binderInfo =>
      Json.arr #["forall", reprStr binderInfo, encodeExpr type, encodeExpr body]
  | .letE _ type value body nondep =>
      Json.arr #["let", toJson nondep, encodeExpr type, encodeExpr value, encodeExpr body]
  | .lit literal => Json.arr #["lit", reprStr literal]
  | .mdata _ expression => encodeExpr expression
  | .proj typeName index value =>
      Json.arr #["proj", typeName.toString, toJson index, encodeExpr value]

private def declarationKind : ConstantInfo → String
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "definition"
  | .thmInfo _ => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quotient"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"

private def sortedNames (names : Array Name) : Array Name :=
  names.qsort fun left right => left.toString < right.toString

private def directDependencies (info : ConstantInfo) : Array Name := Id.run do
  let mut result := #[]
  for name in info.getUsedConstantsAsSet do
    if name != info.name then
      result := result.push name
  return sortedNames result

private def encodeNames (names : Array Name) : Json :=
  Json.arr <| names.map fun name => toJson name.toString

private def encodeDeclaration (info : ConstantInfo) : Elab.Command.CommandElabM Json := do
  let axioms ← collectAxioms info.name
  let value := info.value? (allowOpaque := true)
  return Json.mkObj [
    ("name", info.name.toString),
    ("kind", declarationKind info),
    ("type_expr", encodeExpr info.type),
    ("value_expr", value.map encodeExpr |>.getD Json.null),
    ("direct_dependencies", encodeNames <| directDependencies info),
    ("axioms", encodeNames <| sortedNames axioms)
  ]

private def extract : Elab.Command.CommandElabM Json := do
  let environment ← getEnv
  let namespacePrefix := `ManuscriptVerification
  let declarations := (environment.constants.toList.filter fun pair =>
      namespacePrefix.isPrefixOf pair.1).toArray.qsort fun left right =>
        left.1.toString < right.1.toString
  let mut encoded := #[]
  for (_, info) in declarations do
    encoded := encoded.push (← encodeDeclaration info)
  return Json.mkObj [
    ("schema_version", toJson 1),
    ("lean_version", Lean.versionString),
    ("declarations", Json.arr encoded)
  ]

run_cmd do
  let payload ← extract
  logInfo m!"REPOPROVER_DEPENDENCIES_JSON:{payload.compress}"

end RepoProverDependencyExtractor
