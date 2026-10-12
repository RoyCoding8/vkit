#include "llvm/Support/JSON.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/InstIterator.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IRReader/IRReader.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/IR/Verifier.h"

#include <algorithm>
#include <cstdint>
#include <functional>
#include <iostream>
#include <string>
#include <vector>

using namespace llvm;

namespace {
constexpr const char *VERSION = "vkit-alive2-validate 1 LLVM 20.1.8\n";

json::Object invalid(StringRef reason) {
  return json::Object({{"valid", false}, {"reason", reason.str()}});
}

bool scalarInteger(Type *type) {
  return type->isIntegerTy() && type->getIntegerBitWidth() >= 1 &&
         type->getIntegerBitWidth() <= 64;
}

bool allowedConstant(Value *value) {
  if (auto *integer = dyn_cast<ConstantInt>(value))
    return scalarInteger(integer->getType());
  return isa<Argument>(value) || isa<Instruction>(value) || isa<BasicBlock>(value);
}

bool acyclic(Function &function) {
  enum class Mark : uint8_t { Visiting, Complete };
  DenseMap<BasicBlock *, Mark> marks;
  std::function<bool(BasicBlock *)> visit = [&](BasicBlock *block) {
    if (auto found = marks.find(block); found != marks.end())
      return found->second == Mark::Complete;
    marks[block] = Mark::Visiting;
    auto *terminator = block->getTerminator();
    for (unsigned index = 0; index < terminator->getNumSuccessors(); ++index) {
      auto *successor = terminator->getSuccessor(index);
      auto found = marks.find(successor);
      if (found != marks.end() && found->second == Mark::Visiting)
        return false;
      if (found == marks.end() && !visit(successor))
        return false;
    }
    marks[block] = Mark::Complete;
    return true;
  };
  for (BasicBlock &block : function)
    if (!marks.count(&block) && !visit(&block))
      return false;
  return true;
}

bool supportedInstruction(Instruction &instruction) {
  SmallVector<std::pair<unsigned, MDNode *>, 4> metadata;
  instruction.getAllMetadataOtherThanDebugLoc(metadata);
  if (!metadata.empty() || instruction.getDebugLoc())
    return false;

  if (auto *binary = dyn_cast<BinaryOperator>(&instruction)) {
    if (!scalarInteger(binary->getType()) || !scalarInteger(binary->getOperand(0)->getType()) ||
        !scalarInteger(binary->getOperand(1)->getType()))
      return false;
    for (unsigned index = 0; index < binary->getNumOperands(); ++index)
      if (!allowedConstant(binary->getOperand(index)))
        return false;
    const unsigned opcode = binary->getOpcode();
    const bool nowrap_allowed = opcode == Instruction::Add || opcode == Instruction::Sub ||
                                opcode == Instruction::Mul || opcode == Instruction::Shl;
    const bool exact_allowed = opcode == Instruction::UDiv || opcode == Instruction::SDiv ||
                               opcode == Instruction::LShr || opcode == Instruction::AShr;
    if ((binary->hasNoSignedWrap() || binary->hasNoUnsignedWrap()) && !nowrap_allowed)
      return false;
    if (binary->isExact() && !exact_allowed)
      return false;
    switch (opcode) {
    case Instruction::Add:
    case Instruction::Sub:
    case Instruction::Mul:
    case Instruction::UDiv:
    case Instruction::SDiv:
    case Instruction::URem:
    case Instruction::SRem:
    case Instruction::Shl:
    case Instruction::LShr:
    case Instruction::AShr:
    case Instruction::And:
    case Instruction::Or:
    case Instruction::Xor:
      return true;
    default:
      return false;
    }
  }
  if (auto *compare = dyn_cast<ICmpInst>(&instruction))
    return scalarInteger(compare->getOperand(0)->getType()) &&
           compare->getOperand(0)->getType() == compare->getOperand(1)->getType() &&
           allowedConstant(compare->getOperand(0)) && allowedConstant(compare->getOperand(1));
  if (auto *select = dyn_cast<SelectInst>(&instruction))
    return select->getCondition()->getType()->isIntegerTy(1) &&
           scalarInteger(select->getType()) &&
           select->getTrueValue()->getType() == select->getFalseValue()->getType() &&
           allowedConstant(select->getCondition()) && allowedConstant(select->getTrueValue()) &&
           allowedConstant(select->getFalseValue());
  if (auto *cast = dyn_cast<CastInst>(&instruction)) {
    const auto opcode = cast->getOpcode();
    return (opcode == Instruction::Trunc || opcode == Instruction::ZExt ||
            opcode == Instruction::SExt) &&
           scalarInteger(cast->getSrcTy()) && scalarInteger(cast->getDestTy()) &&
           cast->getSrcTy() != cast->getDestTy() && allowedConstant(cast->getOperand(0));
  }
  if (auto *phi = dyn_cast<PHINode>(&instruction)) {
    if (!scalarInteger(phi->getType()))
      return false;
    for (Value *incoming : phi->incoming_values())
      if (!allowedConstant(incoming))
        return false;
    return true;
  }
  if (auto *branch = dyn_cast<BranchInst>(&instruction))
    return !branch->isConditional() ||
           (branch->getCondition()->getType()->isIntegerTy(1) &&
            allowedConstant(branch->getCondition()));
  if (auto *ret = dyn_cast<ReturnInst>(&instruction))
    return ret->getReturnValue() == nullptr ||
           (scalarInteger(ret->getReturnValue()->getType()) && allowedConstant(ret->getReturnValue()));
  return false;
}

json::Object validate(StringRef path, StringRef requested) {
  LLVMContext context;
  SMDiagnostic diagnostic;
  auto module = parseIRFile(path, diagnostic, context);
  if (!module)
    return invalid("LLVM IR parser rejected the captured module");
  std::string verifier_errors;
  raw_string_ostream verifier_stream(verifier_errors);
  if (verifyModule(*module, &verifier_stream)) {
    verifier_stream.flush();
    return invalid("LLVM verifier rejected the captured module");
  }
  if (!module->getModuleInlineAsm().empty() || !module->global_empty() ||
      !module->alias_empty() || !module->ifunc_empty() || !module->named_metadata_empty())
    return invalid("globals, aliases, inline assembly, and metadata are unsupported");

  Function *selected = nullptr;
  for (Function &function : *module) {
    if (function.isDeclaration())
      return invalid("function declarations are unsupported");
    if (selected != nullptr)
      return invalid("a module must contain exactly one function definition");
    selected = &function;
  }
  if (selected == nullptr || selected->getName() != requested)
    return invalid("the module must define the requested function exactly once");
  if (!selected->hasExternalLinkage() || selected->getVisibility() != GlobalValue::DefaultVisibility ||
      selected->getDLLStorageClass() != GlobalValue::DefaultStorageClass ||
      selected->getCallingConv() != CallingConv::C || selected->isVarArg() ||
      !scalarInteger(selected->getReturnType()) || selected->arg_size() > 32 ||
      !selected->getAttributes().isEmpty() || selected->hasSection() || selected->hasComdat() ||
      selected->hasPersonalityFn() || selected->hasPrefixData() || selected->hasPrologueData())
    return invalid("function signature or attributes are outside the supported scalar C profile");
  for (Argument &argument : selected->args())
    if (!scalarInteger(argument.getType()) || selected->getAttributes().hasParamAttrs(argument.getArgNo()))
      return invalid("all parameters must be unannotated scalar integers from i1 through i64");

  uint64_t instruction_count = 0;
  uint64_t block_count = 0;
  for (BasicBlock &block : *selected) {
    ++block_count;
    if (block.empty() || !block.getTerminator())
      return invalid("every basic block must have a terminator");
    for (Instruction &instruction : block) {
      ++instruction_count;
      if (!supportedInstruction(instruction))
        return invalid("module contains an unsupported instruction, value, flag, or metadata attachment");
    }
  }
  if (!acyclic(*selected))
    return invalid("looping control flow is unsupported; the accepted CFG must be acyclic");

  std::string signature;
  raw_string_ostream stream(signature);
  selected->getFunctionType()->print(stream);
  stream.flush();
  return json::Object({{"valid", true}, {"function", selected->getName().str()},
                       {"signature", signature}, {"blocks", block_count},
                       {"instructions", instruction_count}, {"target_triple", module->getTargetTriple()},
                       {"data_layout", module->getDataLayoutStr()}});
}
} // namespace

int main(int argc, char **argv) {
  if (argc == 2 && std::string(argv[1]) == "--version") {
    std::cout << VERSION;
    return 0;
  }
  if (argc != 3) {
    std::cerr << "usage: llvm-ir-validator captured.ll function\n";
    return 2;
  }
  auto result = validate(argv[1], argv[2]);
  std::string output;
  raw_string_ostream stream(output);
  stream << json::Value(std::move(result));
  stream.flush();
  std::cout << output << '\n';
  return 0;
}
